"""Publish a scoped iTerm2 user variable without changing titles or profiles."""
import json
import math
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace

from .daemon import control
from .rpc import CodexRPC, codex_binary, home_path
from .statusline import active_count, format_count

RESOURCE = Path(__file__).with_name('resources') / 'iterm_status.js'


@dataclass(frozen=True)
class SessionIdentity:
    session_id: str
    source: str
    launch_token: str = field(default=None, repr=False)


class SessionIdentityError(ValueError):
    def __init__(self, message, source, reason):
        super().__init__(message)
        self.details = {'code': 'iterm_identity_invalid', 'source': source, 'reason': reason}


class SessionMismatchError(RuntimeError):
    def __init__(self, expected, observed):
        super().__init__('Current iTerm2 session mismatch: expected {}, observed {}; nothing changed'.format(
            expected, observed))
        self.details = {'code': 'iterm_session_mismatch', 'expected_session_id': expected,
                        'observed_session_id': observed, 'source': 'unspecified'}


def _canonical_session(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('An exact iTerm2 session UUID is required')
    return str(uuid.UUID(value.strip().rsplit(':', 1)[-1])).upper()


def session_id(value=None):
    if value is None:
        value = os.environ.get('ITERM_SESSION_ID')
    if value is None:
        raise ValueError('ITERM_SESSION_ID is missing; run from the intended iTerm2 shell')
    return _canonical_session(value)


def _candidate(value, present):
    result = {'present': present, 'value': value if isinstance(value, str) else None,
              'session_id': None, 'valid_uuid': False}
    if present:
        try:
            result.update(session_id=_canonical_session(value), valid_uuid=True)
        except ValueError:
            pass
    return result


def _scoped_identity(root):
    """Inspect an existing launch lease without creating directories or locks."""
    scoped_present = 'CODEX_MONITOR_ITERM_SESSION' in os.environ
    token_present = 'CODEX_MONITOR_LAUNCH_TOKEN' in os.environ
    candidate = _candidate(os.environ.get('CODEX_MONITOR_ITERM_SESSION'), scoped_present)
    info = {'session': candidate, 'token_present': token_present, 'lease_valid': False,
            'validation_reason': 'absent'}
    if not scoped_present and not token_present:
        return None, info
    if not scoped_present or not token_present:
        info['validation_reason'] = 'partial_markers'
        return None, info
    if not candidate['valid_uuid']:
        info['validation_reason'] = 'invalid_scoped_session'
        return None, info
    token = os.environ.get('CODEX_MONITOR_LAUNCH_TOKEN')
    if not isinstance(token, str) or len(token) != 32:
        info['validation_reason'] = 'invalid_launch_token'
        return None, info
    try:
        uuid.UUID(hex=token)
    except ValueError:
        info['validation_reason'] = 'invalid_launch_token'
        return None, info
    root = Path(root)
    try:
        metadata = root.stat()
    except OSError:
        info['validation_reason'] = 'state_directory_unavailable'
        return None, info
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        info['validation_reason'] = 'state_directory_not_private'
        return None, info
    from .launch_state import LEASE_MAX_AGE, read_lease, valid_lease
    identifier = candidate['session_id']
    lease = read_lease(root, identifier)
    if lease is None:
        info['validation_reason'] = 'lease_missing_or_malformed'
    elif lease.get('token') != token:
        info['validation_reason'] = 'lease_token_mismatch'
    elif lease.get('active') is not True:
        info['validation_reason'] = 'lease_inactive'
    elif valid_lease(root, identifier, token):
        info.update(lease_valid=True, validation_reason='valid')
        return SessionIdentity(identifier, 'validated_launch', token), info
    else:
        info['validation_reason'] = 'lease_invalid_or_owner_unavailable'
        updated = lease.get('updated_at')
        if type(updated) in (int, float):
            try:
                age = time.time() - float(updated)
                if math.isfinite(age) and age > LEASE_MAX_AGE:
                    info['validation_reason'] = 'lease_expired'
                elif math.isfinite(age) and age < 0:
                    info['validation_reason'] = 'lease_future_timestamp'
            except (TypeError, ValueError, OverflowError):
                pass
    return None, info


def resolve_session(root, explicit=None):
    """Choose a candidate by provenance; observing GUI focus is never selection."""
    if explicit is not None:
        try:
            identifier = _canonical_session(explicit)
        except ValueError as exc:
            raise SessionIdentityError('Invalid explicit --session UUID; nothing changed',
                                       'explicit', 'invalid_explicit_session') from exc
        scoped, _ = _scoped_identity(root)
        token = scoped.launch_token if scoped and scoped.session_id == identifier else None
        return SessionIdentity(identifier, 'explicit', token)
    scoped, info = _scoped_identity(root)
    if scoped:
        return scoped
    if info['validation_reason'] != 'absent':
        raise SessionIdentityError('Invalid scoped Codex terminal identity: {}; no legacy fallback'.format(
            info['validation_reason']), 'validated_launch', info['validation_reason'])
    try:
        return SessionIdentity(session_id(), 'legacy_environment')
    except ValueError as exc:
        raise SessionIdentityError('ITERM_SESSION_ID is missing or invalid; use iterm identify for diagnostics',
                                   'legacy_environment', 'legacy_session_missing_or_invalid') from exc


def identify_session(root, explicit=None):
    """Report provenance and current GUI observation, with no ownership claim."""
    _, scoped = _scoped_identity(root)
    provenance = {'explicit': _candidate(explicit, explicit is not None),
                  'scoped_launch': scoped,
                  'legacy_environment': _candidate(os.environ.get('ITERM_SESSION_ID'),
                                                   'ITERM_SESSION_ID' in os.environ)}
    try:
        identity = resolve_session(root, explicit)
        selection = {'session_id': identity.session_id, 'source': identity.source, 'valid': True,
                     'validated_launch_lease': bool(identity.launch_token),
                     'requires_current_session_guard': True}
    except SessionIdentityError as exc:
        selection = dict(exc.details, session_id=None, valid=False)
    observed = action('identify', None)
    from .launch_state import lease_summary
    return {'read_only': True, 'state_dir': str(Path(root)), 'identity': selection, 'provenance': provenance,
            'observed_current_session': observed,
            'observed_launch': lease_summary(root, observed['session_id']),
            'matches_observed_current': bool(selection['valid'] and
                selection['session_id'] == observed.get('session_id')),
            'gui_observation_is_ownership_proof': False,
            'note': 'GUI focus is diagnostic only. Confirm a terminal explicitly with --session; nothing is changed.'}


def _mismatch_context(error, identity, root, operation, args=None):
    command = ['codex-monitor', '--state-dir', str(root), 'iterm', operation,
               '--session', error.details['observed_session_id']]
    if operation == 'attach' and args is not None:
        if getattr(args, 'thread', None):
            command += ['--thread', args.thread]
        if getattr(args, 'codex_home', None):
            command += ['--codex-home', home_path(args.codex_home)]
    error.details.update(source=identity.source, confirmation_command=shlex.join(command))
    return error


def action(name, expected, payload=None):
    if sys.platform != 'darwin':
        raise ValueError('iTerm2 integration requires macOS')
    result = subprocess.run(['/usr/bin/osascript', '-l', 'JavaScript', str(RESOURCE),
                             name, expected if expected is not None else '', json.dumps(payload or {})],
                            capture_output=True, text=True, timeout=8)
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[:1000])
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise RuntimeError('The iTerm2 session bridge returned an invalid response')
    error = value.get('error') if isinstance(value, dict) else None
    if isinstance(error, dict) and error.get('code') == 'iterm_session_mismatch':
        raise SessionMismatchError(_canonical_session(error.get('expected_session_id')),
                                   _canonical_session(error.get('observed_session_id')))
    return value


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
    return count, format_count(count)


def attachment_path(root, identifier):
    return root / 'iterm' / (identifier + '.json')


def attach(root, args):
    internal_token = getattr(args, 'launch_token', None)
    identity = (SessionIdentity(session_id(args.session), 'validated_launch', internal_token)
                if internal_token else resolve_session(root, args.session))
    identifier = identity.session_id
    thread = args.thread
    if not thread:
        raise ValueError('CODEX_THREAD_ID or --thread is required')
    home = home_path(args.codex_home)
    if identity.launch_token and not internal_token:
        try:
            result = attach_for_launch(root, thread, home, identity=identity)
        except SessionMismatchError as exc:
            raise _mismatch_context(exc, identity, root, 'attach', args)
        if not result or (result.get('attached') is not True and result.get('managed_by') != 'launch_client'):
            if result and result.get('reason') == 'explicit_confirmation_required':
                error = SessionIdentityError('This legacy launch requires an explicit --session confirmation; nothing changed',
                                             identity.source, 'explicit_confirmation_required')
                error.details.update(code='iterm_explicit_confirmation_required',
                                     context=result['context'], session_id=result['session_id'],
                                     confirmation_command=result['confirmation_command'])
                raise error
            raise SessionIdentityError('Codex launch identity expired or changed before attachment; nothing changed',
                                       identity.source, 'lease_invalid_before_attach')
        return dict(result, session_id=identifier, thread_id=thread, codex_home=home,
                    identity_source=identity.source)
    # An explicitly confirmed terminal can already be owned by its local
    # launcher even when the shared backend carries another client's markers.
    # Do not create a second writer competing with that launcher's counter.
    from .launch_state import read_lease, valid_lease
    existing = read_lease(root, identifier)
    if (existing and existing.get('counter_manager') == 'launch_client'
            and valid_lease(root, identifier, existing.get('token'))):
        counter = existing.get('counter') or {}
        return {'attached': counter.get('state') == 'bound' and
                           counter.get('thread_id') == thread.lower() and
                           counter.get('codex_home') == home,
                'managed_by': 'launch_client', 'session_id': identifier,
                'state': counter.get('state', 'awaiting_client_identity'),
                'identity_source': identity.source}
    try:
        metadata = action('probe', identifier)
    except SessionMismatchError as exc:
        raise _mismatch_context(exc, identity, root, 'attach', args)
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
    if internal_token:
        from .launch_state import valid_lease
        if not valid_lease(root, identifier, internal_token):
            raise SessionIdentityError('Codex launch ended before terminal mutation; nothing changed',
                                       identity.source, 'lease_invalid_before_mutation')
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
        if isinstance(primary, SessionMismatchError):
            # A rejected variable operation did not mutate this or another shell.
            path.unlink(missing_ok=True)
            raise _mismatch_context(primary, identity, root, 'attach', args)
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
            'identity_source': identity.source,
            'display_expression': r'\(user.codexMonitor)'}


def _record_action_error(plan, error, stage='paused'):
    plan.update(stage=stage, last_error=str(error)[:500], updated_at=time.time())
    if isinstance(error, SessionMismatchError):
        plan['last_error_details'] = dict(error.details, source='attachment',
            bound_session_id=plan['session_id'],
            resume_hint='Return focus to the originally bound iTerm2 session; the target will not change.')
    else:
        plan.pop('last_error_details', None)


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
                    except (RuntimeError, subprocess.TimeoutExpired) as exc:
                        _record_action_error(plan, exc, stage='awaiting_focus_to_clear')
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
                plan.pop('last_error_details', None)
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                # Focus may belong to a different shell. Never find/select/write it.
                _record_action_error(plan, exc)
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


def attach_for_launch(root, thread, home, identity=None):
    """Inspect a native manager or perform explicitly confirmed legacy binding."""
    token = os.environ.get('CODEX_MONITOR_LAUNCH_TOKEN')
    expected = os.environ.get('CODEX_MONITOR_ITERM_SESSION')
    if 'CODEX_MONITOR_LAUNCH_TOKEN' not in os.environ and 'CODEX_MONITOR_ITERM_SESSION' not in os.environ:
        return None
    if not token or not expected:
        return {'attached': False, 'reason': 'Incomplete scoped Codex terminal identity; no legacy fallback'}
    from .launch_state import launch_lock, read_lease, valid_lease
    identifier = session_id(expected)
    if identity is not None and (identifier != identity.session_id or token != identity.launch_token):
        return {'attached': False, 'reason': 'Codex launch identity changed before attachment'}

    def matching_legacy_attachment(path):
        try:
            plan = json.loads(path.read_text())
            if not isinstance(plan, dict):
                return False
            updated = plan.get('updated_at')
            if type(updated) not in (int, float):
                return False
            age = time.time() - float(updated)
            return (0 <= age < 10 and plan.get('active') is True and
                    plan.get('thread_id') == thread and plan.get('codex_home') == home and
                    plan.get('launch_token') == token)
        except (OSError, ValueError, TypeError, OverflowError):
            return False

    def legacy_confirmation_required():
        command = ['codex-monitor', '--state-dir', str(root), 'iterm', 'attach',
                   '--session', identifier, '--thread', thread, '--codex-home', home_path(home)]
        return {'attached': False, 'reason': 'explicit_confirmation_required',
                'context': 'legacy_client_context', 'session_id': identifier,
                'thread_id': thread, 'confirmation_command': shlex.join(command)}

    # A live old launch token can be inherited by another Codex client while
    # its original owner is still running. It proves lease liveness, not the
    # origin of this watch request. Legacy automatic callers may only inspect
    # an already matching binding; creating/changing one needs --session.
    confirmed = identity is not None and identity.source == 'explicit'
    if not confirmed:
        scoped, _ = _scoped_identity(root)
        if scoped is None or scoped.session_id != identifier or scoped.launch_token != token:
            return {'attached': False, 'reason': 'Codex launch lease has expired'}
        observed_lease = read_lease(root, identifier)
        if observed_lease is None:
            return {'attached': False, 'reason': 'Codex launch lease has expired'}
        if observed_lease.get('counter_manager') != 'launch_client':
            if matching_legacy_attachment(attachment_path(root, identifier)):
                return {'attached': True, 'already_attached': True}
            return legacy_confirmation_required()
    with launch_lock(root, identifier):
        if not valid_lease(root, identifier, token):
            return {'attached': False, 'reason': 'Codex launch lease has expired'}
        lease = read_lease(root, identifier)
        if lease.get('counter_manager') == 'launch_client':
            counter = lease.get('counter') or {}
            matches = (counter.get('state') == 'bound' and
                       counter.get('thread_id') == thread.lower() and
                       counter.get('codex_home') == home_path(home))
            return {'attached': matches, 'managed_by': 'launch_client',
                    'session_id': identifier, 'thread_id': thread,
                    'state': counter.get('state', 'awaiting_client_identity'),
                    'note': 'The owning launcher restores the counter independently of tool environment.'}
        path = attachment_path(root, identifier)
        if matching_legacy_attachment(path):
            return {'attached': True, 'already_attached': True}
        if not confirmed:
            # Recheck after acquiring the lock if an ownership record changed
            # between the read-only inspection and this critical section.
            return legacy_confirmation_required()
        if path.exists():
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
    if args.iterm_action == 'identify':
        return identify_session(root, args.session)
    identity = resolve_session(root, args.session)
    identifier = identity.session_id
    try:
        if args.iterm_action == 'detach':
            return dict(detach(root, identifier), identity_source=identity.source)
        metadata = action('probe', identifier)
        if args.iterm_action == 'api-status':
            from .launch import profile_api
            return {'current_session': metadata, 'api_available': True,
                    'identity_source': identity.source,
                    'profile': profile_api(identifier, 'get')}
        path = attachment_path(root, identifier)
        from .launch_state import lease_summary
        return {'current_session': metadata, 'identity_source': identity.source,
                'attachment': json.loads(path.read_text()) if path.exists() else None,
                'launch': lease_summary(root, identifier)}
    except SessionMismatchError as exc:
        raise _mismatch_context(exc, identity, root, args.iterm_action, args)
