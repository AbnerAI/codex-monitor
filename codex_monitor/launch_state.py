"""Private, token-scoped leases for an interactive Codex terminal launch."""
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import time
import uuid

from .iterm import save

LEASE_MAX_AGE = 120


def lease_path(root, identifier):
    identifier = str(uuid.UUID(identifier)).upper()
    return Path(root) / 'iterm-launches' / (identifier + '.json')


@contextmanager
def launch_lock(root, identifier):
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('The monitor state directory must be private to its owner')
    path = lease_path(root, identifier).with_suffix('.lock')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def read_lease(root, identifier):
    try:
        value = json.loads(lease_path(root, identifier).read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def lease_summary(root, identifier):
    """Read existing ownership/counter diagnostics without disclosing its nonce."""
    try:
        info = Path(root).stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            return None
    except OSError:
        return None
    value = read_lease(root, identifier)
    if value is None:
        return None
    counter = value.get('counter')
    return {'session_id': identifier, 'active': valid_lease(root, identifier, value.get('token')),
            'counter_manager': value.get('counter_manager', 'watch_worker'),
            'counter': {key: counter.get(key) for key in (
                'state', 'thread_id', 'codex_home', 'active', 'label', 'native_prefix')}
                if isinstance(counter, dict) else None}


def valid_lease(root, identifier, token, now=None):
    if not isinstance(token, str) or len(token) != 32:
        return False
    try:
        uuid.UUID(hex=token)
    except ValueError:
        return False
    value = read_lease(root, identifier)
    if not value or value.get('active') is not True or value.get('token') != token:
        return False
    pid, updated = value.get('pid'), value.get('updated_at')
    if type(pid) is not int or pid <= 1 or type(updated) not in (int, float):
        return False
    if not isinstance(value.get('session_id'), str):
        return False
    try:
        if str(uuid.UUID(value.get('session_id', ''))).upper() != str(uuid.UUID(identifier)).upper():
            return False
        updated = float(updated)
        if not math.isfinite(updated):
            return False
        age = (time.time() if now is None else now) - updated
    except (ValueError, TypeError, OverflowError):
        return False
    # Binding can legitimately hold the per-session lock through bounded RPC
    # and AppleEvent waits. PID death still invalidates the lease immediately.
    if not 0 <= age <= LEASE_MAX_AGE:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def create_lease(root, identifier, token):
    path = lease_path(root, identifier)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    existing = read_lease(root, identifier)
    if existing and valid_lease(root, identifier, existing.get('token')):
        raise ValueError('A Codex launch already owns this terminal')
    stamp = time.time()
    value = {'active': True, 'token': token, 'session_id': identifier,
             'pid': os.getpid(), 'started_at': stamp, 'updated_at': stamp}
    save(path, value)
    return value


def refresh_lease(root, identifier, token):
    value = read_lease(root, identifier)
    if value and value.get('active') is True and value.get('token') == token:
        value['updated_at'] = time.time()
        save(lease_path(root, identifier), value)


def invalidate_lease(root, identifier, token):
    value = read_lease(root, identifier)
    if value and value.get('token') == token:
        value.update(active=False, updated_at=time.time())
        save(lease_path(root, identifier), value)
        return True
    return False
