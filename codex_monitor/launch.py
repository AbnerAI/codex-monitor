"""Opt-in decoration of an interactive Codex launch in its own iTerm2 session."""
import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

from .iterm import action, attachment_path, save, session_id, stop_owned_launch_updater, stop_updater
from .launch_state import (create_lease, invalidate_lease, launch_lock, lease_path,
                           read_lease, refresh_lease, valid_lease)
from .client_counter import resolve_counter
from .client_home import discover_client_home
from .daemon import control

EXPRESSION = r'\(user.codexMonitor)'
TEXT_COLOR_KEY = 'shared text color'
# Native iTerm2 color dictionary, sRGB #00CC66 (not an ANSI escape).
DEFAULT_TEXT_COLOR = {'Color Space': 'sRGB', 'Red Component': 0.0,
                      'Green Component': 0.8, 'Blue Component': 0.4,
                      'Alpha Component': 1.0}
COMPONENT = {'class': 'iTermStatusBarSwiftyStringComponent',
             'configuration': {'knobs': {'expression': EXPRESSION,
                                          TEXT_COLOR_KEY: DEFAULT_TEXT_COLOR}}}
VALUE_OPTIONS = {'-c', '--config', '--enable', '--disable', '--remote',
                 '--remote-auth-token-env', '-m', '--model', '--local-provider',
                 '-p', '--profile', '-s', '--sandbox', '-C', '--cd', '--add-dir',
                 '-a', '--ask-for-approval', '-i', '--image'}
NONINTERACTIVE = {'agents', 'exec', 'e', 'review', 'login', 'logout', 'mcp', 'plugin',
                  'app-server', 'remote-control', 'app', 'completion', 'update',
                  'doctor', 'sandbox', 'debug', 'apply', 'a', 'queue', 'archive',
                  'delete', 'migrate-rollouts', 'migrate-nfs-db', 'unarchive',
                  'cloud', 'exec-server', 'features', 'help'}


def interactive_arguments(arguments):
    """Identify local TUI launches without confusing option values with commands."""
    positional = None
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == '--':
            break
        if value in ('--help', '-h', '--version', '-V', '--no-daemon'):
            return False
        if value == '--remote' or value.startswith('--remote='):
            return False
        if value in VALUE_OPTIONS:
            index += 2
            continue
        if value.startswith('-'):
            index += 1
            continue
        if positional is None:
            positional = value
        index += 1
    return positional not in NONINTERACTIVE


def api_python():
    explicit = os.environ.get('CODEX_MONITOR_ITERM_PYTHON')
    candidates = [explicit,
                  str(Path(__file__).resolve().parent.parent / 'iterm-runtime/bin/python'),
                  str(Path.home() / '.local/share/codex-monitor/iterm-runtime/bin/python')]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    if importlib.util.find_spec('iterm2') is not None:
        return sys.executable
    raise RuntimeError('Install the optional iterm2 client in the monitor iTerm runtime first')


def profile_api(identifier, operation, properties=None):
    suite = os.environ.get('IT2_SUITE', 'iTerm2')
    endpoint = Path.home() / 'Library/Application Support' / suite / 'private/socket'
    if not endpoint.exists():
        raise RuntimeError('Enable iTerm2 Settings > General > Magic > Python API once to use automatic setup')
    helper = Path(__file__).with_name('resources') / 'iterm_profile_api.py'
    request = {'action': operation, 'session_id': identifier}
    if properties is not None:
        request['properties'] = properties
    completed = subprocess.run([api_python(), '-B', str(helper)],
                               input=json.dumps(request), capture_output=True,
                               text=True, timeout=8)
    try:
        reply = json.loads(completed.stdout)
    except ValueError as exc:
        raise RuntimeError('The iTerm2 profile client did not return a valid response') from exc
    if completed.returncode or reply.get('ok') is not True:
        raise RuntimeError(reply.get('error', 'The iTerm2 profile client is unavailable'))
    return reply['result']


def augmented_profile(original):
    if (not isinstance(original, dict) or type(original.get('enabled')) is not bool
            or original.get('layout') is not None and not isinstance(original['layout'], dict)):
        raise ValueError('Cannot safely capture the existing iTerm2 status bar')
    layout = copy.deepcopy(original['layout']) if original['layout'] is not None else {'components': []}
    if not isinstance(layout.get('components'), list):
        raise ValueError('Unsupported status bar layout; leaving it unchanged')
    for component in layout['components']:
        if not isinstance(component, dict):
            raise ValueError('Unsupported status bar component; leaving it unchanged')
        configuration = component.get('configuration')
        knobs = configuration.get('knobs') if isinstance(configuration, dict) else None
        if (component.get('class') == COMPONENT['class'] and isinstance(knobs, dict)
                and knobs.get('expression') == EXPRESSION):
            if knobs.get(TEXT_COLOR_KEY) is None:
                knobs[TEXT_COLOR_KEY] = copy.deepcopy(DEFAULT_TEXT_COLOR)
            return {'layout': layout, 'enabled': True}
    layout['components'].append(copy.deepcopy(COMPONENT))
    return {'layout': layout, 'enabled': True}


def apply_profile(identifier, value):
    return profile_api(identifier, 'apply', {'Status Bar Layout': value['layout'],
                                             'Show Status Bar': value['enabled']})


def _write_terminal(descriptor, data):
    # This is a duplicate of the launcher's actual terminal FD, never a reopened
    # cached tty path. It still targets that terminal when focus moves elsewhere.
    while data:
        try:
            written = os.write(descriptor, data)
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError('Terminal write made no progress')
        data = data[written:]


def publish_terminal_variable(descriptor, label):
    _write_terminal(descriptor, b'\x1b]1337;SetUserVar=codexMonitor=' +
                    base64.b64encode(label.encode('utf-8')) + b'\x07')


def clear_terminal_variable(descriptor):
    publish_terminal_variable(descriptor, '')


def _write_terminal_title(descriptor, title):
    if not isinstance(title, str) or len(title) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in title):
        raise ValueError('Cannot safely restore terminal title text')
    _write_terminal(descriptor, b'\x1b]0;' + title.encode('utf-8') + b'\x07')


def launch_session(descriptor):
    """Prove launch origin with its real terminal FD, never GUI focus alone."""
    observed = action('identify', None)
    if (not os.isatty(descriptor) or observed.get('tty') != os.ttyname(descriptor)
            or observed.get('panes') != 1):
        raise ValueError('The focused iTerm2 session does not match the launching terminal FD')
    identifier = observed.get('session_id')
    if not isinstance(identifier, str) or not identifier:
        raise ValueError('iTerm2 did not report an exact launching session UUID')
    return session_id(identifier)


class Decoration:
    def __init__(self, root, identifier, descriptor, client_counter=False, codex_home=None):
        self.root, self.identifier, self.descriptor = root, identifier, descriptor
        self.token = uuid.uuid4().hex
        self.original = self.applied = None
        self.created = False
        self.ready = False
        self.client_counter = client_counter
        self.codex_home = codex_home
        self.last_label = ''
        self.original_native_title = None
        self.starting_title = 'codex-monitor: starting ' + uuid.uuid4().hex[:12]

    def prepare(self):
        if self.client_counter and not self.codex_home:
            raise ValueError('The launching Codex profile has not been established')
        metadata = action('probe', self.identifier)
        if not os.isatty(self.descriptor) or os.ttyname(self.descriptor) != metadata['tty']:
            raise ValueError('Terminal FD does not belong to the authorized iTerm2 session')
        with launch_lock(self.root, self.identifier):
            previous = read_lease(self.root, self.identifier)
            if previous and valid_lease(self.root, self.identifier, previous.get('token')):
                raise ValueError('Another interactive Codex launch owns this terminal')
            self.original = profile_api(self.identifier, 'get')
            # A hard-killed previous launcher may have left only its blank bar.
            if (previous and previous.get('original') is not None
                    and previous.get('applied') == self.original):
                stop_owned_launch_updater(self.root, self.identifier, previous['token'])
                self.original = apply_profile(self.identifier, previous['original'])
            self.applied = augmented_profile(self.original)
            create_lease(self.root, self.identifier, self.token)
            self.created = True
            path = lease_path(self.root, self.identifier)
            lease = read_lease(self.root, self.identifier)
            lease.update(original=self.original, applied=self.applied,
                         counter_manager='launch_client' if self.client_counter else 'watch_worker')
            save(path, lease)
            # Retire a manually attached updater in this same terminal. Watchers
            # remain active and keep notifying their original conversations.
            attachment = attachment_path(self.root, self.identifier)
            if attachment.exists():
                stop_updater(attachment)
                os.replace(attachment, attachment.with_suffix('.detached.json'))
                attachment.with_suffix('.stop').unlink(missing_ok=True)
            clear_terminal_variable(self.descriptor)
            readback = apply_profile(self.identifier, self.applied)
            if readback != self.applied:
                raise RuntimeError('iTerm2 did not retain the requested current-session layout')
            if self.client_counter:
                self.original_native_title = profile_api(self.identifier, 'identity').get('native_title')
                # A stale title from an earlier CLI must not count as evidence
                # for this launch. The new official client emits its own ID.
                _write_terminal_title(self.descriptor, self.starting_title)
                deadline = time.monotonic() + 2
                while profile_api(self.identifier, 'identity').get('native_title') != self.starting_title:
                    if time.monotonic() >= deadline:
                        raise RuntimeError('iTerm2 has not confirmed this launch identity epoch')
                    time.sleep(0.03)
            self.ready = True
        return self

    def heartbeat(self):
        with launch_lock(self.root, self.identifier):
            refresh_lease(self.root, self.identifier, self.token)

    def update_counter(self):
        if not self.client_counter:
            return
        try:
            native = profile_api(self.identifier, 'identity').get('native_title')
            rows = control(self.root, {'method': 'list'}, timeout=1)
            if not isinstance(rows, list):
                raise ValueError('The monitor registry returned an invalid watcher list')
            result = resolve_counter(native, rows, self.codex_home)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            result = {'state': 'counter_unavailable', 'thread_id': None, 'codex_home': None,
                      'active': None, 'label': '', 'native_prefix': None}
        with launch_lock(self.root, self.identifier):
            if not valid_lease(self.root, self.identifier, self.token):
                return
            lease = read_lease(self.root, self.identifier)
            lease['counter'] = result
            save(lease_path(self.root, self.identifier), lease)
            if result['label'] != self.last_label:
                publish_terminal_variable(self.descriptor, result['label'])
                self.last_label = result['label']

    def close(self):
        if not self.created:
            return
        with launch_lock(self.root, self.identifier):
            if not invalidate_lease(self.root, self.identifier, self.token):
                return  # A stale launcher cannot clear a newer launch's display.
            stop_owned_launch_updater(self.root, self.identifier, self.token)
            clear_terminal_variable(self.descriptor)
            current = profile_api(self.identifier, 'get')
            if current == self.applied:
                apply_profile(self.identifier, self.original)
            elif not self.ready:
                rollback = {}
                for field, key in (('layout', 'Status Bar Layout'), ('enabled', 'Show Status Bar')):
                    if current.get(field) == self.applied.get(field) and current.get(field) != self.original.get(field):
                        rollback[key] = self.original[field]
                if rollback:
                    profile_api(self.identifier, 'apply', rollback)
            if self.client_counter and self.original_native_title is not None:
                current_title = profile_api(self.identifier, 'identity').get('native_title')
                if current_title == self.starting_title:
                    _write_terminal_title(self.descriptor, self.original_native_title)
            # If the user edited their layout while Codex ran, preserve it.


def child_status(code):
    return code if code >= 0 else 128 - code


def wait_child(command, environment, decoration=None):
    process = None
    saved = {}

    def forward(signum, frame):
        if process is not None and process.poll() is None and signum != signal.SIGINT:
            process.send_signal(signum)

    # The child keeps the existing foreground process group and inherits the
    # real terminal. Ctrl-C reaches it directly; the parent waits for its exit.
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        saved[signum] = signal.signal(signum, forward)
    try:
        process = subprocess.Popen(command, env=environment)
        while True:
            try:
                return child_status(process.wait(timeout=1))
            except subprocess.TimeoutExpired:
                if decoration:
                    try:
                        decoration.heartbeat()
                        decoration.update_counter()
                    except (OSError, ValueError):
                        pass
    finally:
        for signum, handler in saved.items():
            signal.signal(signum, handler)


def launch(root, executable, arguments):
    arguments = list(arguments)
    if arguments[:1] == ['--']:
        arguments.pop(0)
    command = [executable] + arguments
    environment = os.environ.copy()
    enabled = (sys.platform == 'darwin' and environment.get('TERM_PROGRAM') == 'iTerm.app'
               and not environment.get('CODEX_MONITOR_LAUNCH_ACTIVE')
               and os.isatty(0) and os.isatty(1) and interactive_arguments(arguments))
    if not enabled:
        return wait_child(command, environment)
    descriptor = os.dup(1)
    decoration = None
    try:
        if os.ttyname(0) != os.ttyname(descriptor):
            return wait_child(command, environment)
        try:
            identifier = launch_session(descriptor)
            codex_home = discover_client_home(executable, arguments)
            decoration = Decoration(root, identifier, descriptor, client_counter=True,
                                    codex_home=codex_home)
            decoration.prepare()
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            if decoration is not None:
                try:
                    decoration.close()
                except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                    pass
            print('codex-monitor: automatic indicator unavailable: ' + str(exc), file=sys.stderr)
            decoration = None
        if decoration is not None:
            environment['CODEX_MONITOR_LAUNCH_ACTIVE'] = decoration.token
            # Codex 0.166 excludes shared-daemon use when CLI config overrides
            # contain shell_environment_policy or tui.terminal_title. Never
            # inject -c here: the running TUI and monitor must share a runtime.
            # Native title markers are configured once, via explicit opt-in.
        return wait_child(command, environment, decoration)
    finally:
        try:
            if decoration is not None:
                decoration.close()
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            print('codex-monitor: indicator cleanup incomplete: ' + str(exc), file=sys.stderr)
        finally:
            os.close(descriptor)
