"""Publish a scoped iTerm2 user variable without changing titles or profiles."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
from types import SimpleNamespace

from .daemon import control
from .rpc import CodexRPC, codex_binary, home_path
from .statusline import active_count

RESOURCE = Path(__file__).with_name('resources') / 'iterm_status.js'


def session_id(value=None):
    value = value or os.environ.get('ITERM_SESSION_ID')
    if not value:
        raise ValueError('ITERM_SESSION_ID is missing; run from the intended iTerm2 shell')
    return str(uuid.UUID(value.rsplit(':', 1)[-1])).upper()


def action(name, expected, payload=None):
    if sys.platform != 'darwin':
        raise ValueError('iTerm2 integration requires macOS')
    result = subprocess.run(['/usr/bin/osascript', '-l', 'JavaScript', str(RESOURCE),
                             name, expected, json.dumps(payload or {})],
                            capture_output=True, text=True, timeout=8)
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[:1000])
    return json.loads(result.stdout)


def save(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def label_for_rows(rows, thread, home):
    watchers = [{'active': row.get('active'), 'spec': {
        'thread_id': row.get('thread_id'), 'codex_home': row.get('codex_home')}} for row in rows]
    count = active_count(watchers, thread, home)
    return count, 'monitor-' + str(count) if count else ''


def attachment_path(root, identifier):
    return root / 'iterm' / (identifier + '.json')


def attach(root, args):
    identifier = session_id(args.session)
    thread = args.thread
    if not thread:
        raise ValueError('CODEX_THREAD_ID or --thread is required')
    home = home_path(args.codex_home)
    metadata = action('probe', identifier)
    path = attachment_path(root, identifier)
    if path.exists():
        raise ValueError('An attachment record already exists; detach it before rebinding')
    rpc = CodexRPC(home, codex_binary())
    try:
        if rpc.target(thread)['id'] != thread:
            raise ValueError('Codex thread identity mismatch')
    finally:
        rpc.close()
    count, label = label_for_rows(control(root, {'method': 'list'}), thread, home)
    plan = {'session_id': identifier, 'tty': metadata['tty'], 'thread_id': thread,
            'codex_home': home, 'state_dir': str(root),
            'original_format': metadata['title_format'], 'original_monitor': metadata.get('monitor'),
            'active': True, 'label': label,
            'count': count, 'updated_at': time.time(), 'stage': 'attached'}
    if getattr(args, 'launch_token', None):
        plan['launch_token'] = args.launch_token
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    save(path, plan)
    try:
        applied = action('variables', identifier, {'label': label})
        env = os.environ.copy()
        package_root = str(Path(__file__).resolve().parent.parent)
        env['PYTHONPATH'] = package_root + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
        with path.with_suffix('.log').open('ab') as log:
            child = subprocess.Popen([sys.executable, '-B', '-m', 'codex_monitor',
                '--state-dir', str(root), 'iterm', 'run', '--attachment', str(path)],
                env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        plan['pid'] = child.pid
        save(path, plan)
    except Exception as primary:
        rollback_error = None
        try:
            action('variables', identifier, {'label': plan['original_monitor'] or ''})
        except Exception as exc:
            rollback_error = str(exc)
        if rollback_error is None:
            path.unlink(missing_ok=True)
        raise RuntimeError('Attach failed: {}; rollback: {}'.format(primary, rollback_error or 'restored')) from primary
    return {'attached': True, 'session_id': identifier, 'thread_id': thread, 'count': count,
            'label': label, 'tab_title': applied['tab_title'], 'attachment': str(path),
            'scope': 'authorized current session user.codexMonitor only',
            'display_expression': r'\(user.codexMonitor)'}


def run(path):
    plan = json.loads(path.read_text())
    plan['pid'] = os.getpid()
    root = Path(plan['state_dir'])
    stop = path.with_suffix('.stop')
    try:
        while not stop.exists():
            token = plan.get('launch_token')
            if token:
                from .launch_state import read_lease, valid_lease
                lease = read_lease(root, plan['session_id'])
                if lease and lease.get('token') != token:
                    return  # A newer launch owns the terminal now.
                if not valid_lease(root, plan['session_id'], token):
                    try:
                        action('variables', plan['session_id'], {'label': ''})
                        return
                    except (RuntimeError, subprocess.TimeoutExpired):
                        plan.update(stage='awaiting_focus_to_clear', updated_at=time.time())
                        save(path, plan)
                        time.sleep(1)
                        continue
            try:
                rows = control(root, {'method': 'list'}, timeout=3)
                count, label = label_for_rows(rows, plan['thread_id'], plan['codex_home'])
            except (OSError, RuntimeError):
                count, label = None, ''
            if stop.exists():
                break
            try:
                metadata = action('probe', plan['session_id'])
                if stop.exists():
                    break
                if metadata.get('monitor') != label:
                    action('variables', plan['session_id'], {'label': label})
                plan.update(stage='attached', count=count, label=label, updated_at=time.time())
                plan.pop('last_error', None)
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                # Focus may belong to a different shell. Never find/select/write it.
                plan.update(stage='paused', last_error=str(exc)[:500], updated_at=time.time())
            save(path, plan)
            time.sleep(1)
    finally:
        plan.update(active=False, stage='stopped', updated_at=time.time())
        save(path, plan)


def stop_updater(path):
    """Stop this updater cooperatively, without signalling any saved PID."""
    stop = path.with_suffix('.stop')
    stop.touch()
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if not json.loads(path.read_text()).get('active'):
            break
        time.sleep(0.1)
    else:
        raise RuntimeError('Updater is still stopping; retry detach after it stops')


def detach(root, identifier):
    action('probe', identifier)
    path = attachment_path(root, identifier)
    if not path.is_file():
        return {'detached': True, 'already_detached': True}
    plan = json.loads(path.read_text())
    stop_updater(path)
    restored = action('variables', identifier, {'label': plan.get('original_monitor') or ''})
    backup = path.with_suffix('.detached.json')
    os.replace(path, backup)
    path.with_suffix('.stop').unlink(missing_ok=True)
    return {'detached': True, 'session_id': identifier, 'monitor': restored.get('monitor')}


def attach_for_launch(root, thread, home):
    """Optional watch-time binding, with identity supplied by the Codex launcher."""
    token = os.environ.get('CODEX_MONITOR_LAUNCH_TOKEN')
    expected = os.environ.get('CODEX_MONITOR_ITERM_SESSION')
    if not token or not expected:
        return None
    from .launch_state import launch_lock, valid_lease
    identifier = session_id(expected)
    with launch_lock(root, identifier):
        if not valid_lease(root, identifier, token):
            return {'attached': False, 'reason': 'Codex launch lease has expired'}
        path = attachment_path(root, identifier)
        if path.exists():
            plan = json.loads(path.read_text())
            fresh = 0 <= time.time() - plan.get('updated_at', 0) < 10
            if (plan.get('active') and fresh and plan.get('thread_id') == thread
                    and plan.get('codex_home') == home and plan.get('launch_token') == token):
                return {'attached': True, 'already_attached': True}
            detach(root, identifier)
        return attach(root, SimpleNamespace(session=identifier, thread=thread,
                                             codex_home=home, launch_token=token))


def stop_owned_launch_updater(root, identifier, token):
    """Caller holds the launch lock and has revoked this token already."""
    path = attachment_path(root, identifier)
    if not path.exists():
        return
    plan = json.loads(path.read_text())
    if plan.get('launch_token') != token:
        return
    stop_updater(path)
    os.replace(path, path.with_suffix('.detached.json'))
    path.with_suffix('.stop').unlink(missing_ok=True)


def execute(root, args):
    if args.iterm_action == 'run':
        run(Path(args.attachment))
        return {'stopped': True}
    if args.iterm_action == 'attach':
        return attach(root, args)
    identifier = session_id(args.session)
    if args.iterm_action == 'detach':
        return detach(root, identifier)
    metadata = action('probe', identifier)
    if args.iterm_action == 'api-status':
        from .launch import profile_api
        return {'current_session': metadata, 'api_available': True,
                'profile': profile_api(identifier, 'get')}
    path = attachment_path(root, identifier)
    return {'current_session': metadata,
            'attachment': json.loads(path.read_text()) if path.exists() else None}
