#!/usr/bin/env python3
"""Opt-in real CLI transport acceptance, using only an owned test thread.

No model turn is submitted unless --deliver is explicitly supplied. The real Codex CLI resumes a known fixture in a
private PTY through the production launcher. Only iTerm decoration and its
identity/home lookups are replaced; no real GUI/profile is accessed. The fixture
is first archived/unarchived to establish a NOT-loaded baseline, so creating it
through RPC cannot itself make the acceptance check pass.
"""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import sys
import termios
import tempfile
import time
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from codex_monitor.rpc import CodexRPC, codex_binary, home_path

_query_tails = {}


def save(path, value):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Refusing a symlink report path')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open('w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream, indent=2)
        stream.write('\n')


def child(args):
    import codex_monitor.launch as launcher

    class NoGUI:
        def __init__(self, root, identifier, descriptor, **kwargs):
            self.identifier, self.token = identifier, uuid.uuid4().hex

        def prepare(self):
            return self

        def heartbeat(self):
            pass

        def update_counter(self):
            pass

        def close(self):
            pass

    original_wait = launcher.wait_child

    def observed_wait(command, environment, decoration=None):
        save(args.child_report, {'argv': command, 'decorated': decoration is not None,
                                 'actual_cli': True, 'iterm_profile_mocked': True})
        return original_wait(command, environment, decoration)

    # The private PTY is never connected to an iTerm2 window. Keep real CLI
    # execution and production argv construction, replacing only GUI plumbing.
    environment = {'TERM_PROGRAM': 'iTerm.app'}
    with patch.dict(os.environ, environment), \
         patch.object(launcher.sys, 'platform', 'darwin'), \
         patch.object(launcher, 'launch_session', return_value='11111111-2222-4333-8444-555555555555'), \
         patch.object(launcher, 'discover_client_home', return_value=args.codex_home), \
         patch.object(launcher, 'Decoration', NoGUI), \
         patch.object(launcher, 'wait_child', side_effect=observed_wait):
        os.environ.pop('CODEX_MONITOR_LAUNCH_ACTIVE', None)
        return launcher.launch(Path(args.child_state), args.codex_bin,
                               ['resume', args.child_thread])


def drain(descriptor):
    total = 0
    while select.select([descriptor], [], [], 0)[0]:
        try:
            data = os.read(descriptor, 65536)
        except OSError:
            break
        if not data:
            break
        total += len(data)
        payload = _query_tails.get(descriptor, b'') + data
        if b'\x1b[6n' in payload:
            # Only terminal cursor reporting is emulated. Never answer a
            # project-trust, approval, account, or onboarding prompt.
            with contextlib.suppress(OSError):
                os.write(descriptor, b'\x1b[1;1R')
        _query_tails[descriptor] = payload[-3:]
    return total


def stop_child(pid, descriptor):
    if pid is None:
        return None
    # pty.fork created this process group exclusively for our fixture client.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        drain(descriptor)
        found, status = os.waitpid(pid, os.WNOHANG)
        if found:
            return os.waitstatus_to_exitcode(status)
        time.sleep(0.05)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


def canonical_ready(entries, token):
    """Match the actual agent READY and its canonical user prompt in one turn."""
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('turnId'), str):
            continue
        item = entry.get('item', {})
        if (item.get('type') != 'agentMessage' or not isinstance(item.get('id'), str)
                or not isinstance(item.get('text'), str) or token not in item['text']):
            continue
        for prompt in entries:
            if not isinstance(prompt, dict) or prompt.get('turnId') != entry['turnId']:
                continue
            user = prompt.get('item', {})
            if (user.get('type') == 'userMessage' and isinstance(user.get('id'), str)
                    and token in json.dumps(user, ensure_ascii=False)):
                return {'agent': entry, 'user': prompt}
    return None


def snapshot(rpc, thread_id, report):
    """Capture the owned fixture's state before cleanup interrupts its runtime."""
    try:
        thread = rpc.request('thread/read', {'threadId': thread_id, 'includeTurns': True})['thread']
        report['final_thread_status'] = thread.get('status')
        report['final_turns'] = [{'id': turn.get('id'), 'status': turn.get('status'),
                                  'error': turn.get('error')}
                                 for turn in thread.get('turns', [])[-5:]]
    except Exception as exc:
        report['final_snapshot_error'] = str(exc)
    report['rpc_diagnostics'] = [row for row in list(rpc.diagnostics)[-10:]
                                 if row.get('threadId') in (None, thread_id)]


def run(args):
    if not 5 <= args.timeout <= 300:
        raise ValueError('--timeout must be between 5 and 300 seconds per stage')
    binary, home = codex_binary(args.codex_bin), home_path(args.codex_home)
    report = {'passed': False, 'actual_cli': True, 'iterm_profile_mocked': True,
              'transport_verified': False, 'notification_accepted': False,
              'model_ack_verified': False, 'model_turn_submitted': False,
              'warmup_ready': False, 'warmup_turn_submitted': False,
              'timeout_seconds_per_stage': args.timeout, 'stage': 'preparing_fixture',
              'config_changed': False, 'test_thread_archived': False,
              'watchers_created': 0, 'monitor_service_started': False,
              'codex_home': home, 'codex_bin': binary}
    pid = descriptor = thread_id = None
    group_id = None
    rpc = CodexRPC(home, binary, args.socket, timeout=5)
    temporary = tempfile.TemporaryDirectory(
        prefix='cm-route-', dir='/private/tmp' if sys.platform == 'darwin' else None)
    try:
        with contextlib.nullcontext(temporary.name) as temp:
            root = Path(temp).resolve()
            work = root / 'work'
            work.mkdir(mode=0o700)
            cwd = str(Path(args.cwd).expanduser().resolve()) if args.cwd else str(work)
            created = rpc.request('thread/start', {'cwd': cwd}, mutating=True)
            thread_id = created['thread']['id']
            uuid.UUID(thread_id)
            report['thread_id'] = thread_id
            # Own fixture only. Closing/subscribing alone need not unload a
            # cached runtime; archive/unarchive yields an explicit baseline.
            rpc.request('thread/archive', {'threadId': thread_id}, mutating=True)
            rpc.request('thread/unarchive', {'threadId': thread_id}, mutating=True)
            if rpc.runtime_loaded(thread_id):
                raise RuntimeError('Fixture stayed loaded before CLI launch; cannot prove CLI ownership')
            report['not_loaded_before_cli'] = True
            command_report = root / 'actual-cli.json'
            child_args = [sys.executable, '-B', str(Path(__file__).resolve()), '--child',
                          '--codex-bin', binary, '--codex-home', home,
                          '--child-thread', thread_id, '--child-state', str(root / 'state'),
                          '--child-report', str(command_report)]
            pid, descriptor = pty.fork()
            if pid == 0:
                os.chdir(cwd)
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
                os.execv(sys.executable, child_args)
            group_id = pid
            report['stage'] = 'waiting_for_shared_cli'
            deadline = time.monotonic() + args.timeout
            observed_bytes = 0
            while time.monotonic() < deadline:
                observed_bytes += drain(descriptor)
                exited, status = os.waitpid(pid, os.WNOHANG)
                if exited:
                    report['child_exit_code'] = os.waitstatus_to_exitcode(status)
                    pid = None
                    raise RuntimeError('Official CLI exited before shared runtime ownership was verified')
                if command_report.exists():
                    command = json.loads(command_report.read_text())
                    report['actual_argv'] = command['argv']
                    if not command.get('decorated'):
                        raise RuntimeError('Production launcher decoration path was not exercised')
                    if command['argv'] != [binary, 'resume', thread_id]:
                        raise RuntimeError('Launcher modified the official CLI arguments')
                    ready = rpc.readiness(thread_id)
                    report['last_readiness'] = ready
                    if ready['registration_ready']:
                        report.update(transport_verified=True, passed=True,
                                      pty_bytes_observed=observed_bytes)
                        break
                time.sleep(0.2)
            if not report['passed']:
                raise RuntimeError('Timed out: CLI did not load the known fixture on the shared server. '
                                   'Check onboarding/trust prompts or daemon-incompatible launch settings; '
                                   'this check never accepts prompts or changes trust/configuration')
            if args.deliver:
                from session_check import find_agent_ack
                ready_token = 'READY_' + uuid.uuid4().hex
                prompt = ('This is an authorized codex-monitor routing acceptance test. '
                          'Reply with exactly ' + ready_token + ' now, then wait. '
                          'When a later external codex_monitor.monitor_event arrives, reply with '
                          'the acknowledgement token specified in its content. '
                          'Do not call tools or modify files during this test.')
                # Exercise a real user conversation first. An empty fixture
                # receiving only external output is not a faithful idle-wake test.
                report.update(stage='waiting_for_warmup_reply', warmup_turn_submitted=True)
                drain(descriptor)
                os.write(descriptor, b'\x1b[200~' + prompt.encode() + b'\x1b[201~\r')
                deadline = time.monotonic() + args.timeout
                while time.monotonic() < deadline:
                    drain(descriptor)
                    history = rpc.request('thread/items/list', {
                        'threadId': thread_id, 'limit': 100, 'sortDirection': 'desc'})
                    matched = canonical_ready(history.get('data', []), ready_token)
                    if matched:
                        report.update(warmup_ready=True, warmup_token=ready_token,
                                      warmup_turn_id=matched['agent']['turnId'],
                                      warmup_agent_item_id=matched['agent']['item']['id'],
                                      warmup_user_item_id=matched['user']['item']['id'])
                        break
                    time.sleep(0.2)
                if not report['warmup_ready']:
                    raise RuntimeError('No canonical READY reply to the private PTY user prompt '
                                       'was verified before the warmup deadline')
                report['stage'] = 'waiting_for_idle_after_warmup'
                deadline = time.monotonic() + args.timeout
                idle_since = None
                while time.monotonic() < deadline:
                    drain(descriptor)
                    status = rpc.target(thread_id).get('status', {})
                    if status.get('type') == 'idle':
                        idle_since = idle_since or time.monotonic()
                        if time.monotonic() - idle_since >= 0.5:
                            report['pre_delivery_thread_status'] = status
                            report['observed_idle_after_warmup'] = True
                            break
                    else:
                        idle_since = None
                    time.sleep(0.2)
                else:
                    raise RuntimeError('The warmed-up fixture did not become idle before the deadline')
                token = 'ROUTE_ACK_' + uuid.uuid4().hex
                report.update(stage='waiting_for_notification_reply',
                              model_turn_submission_attempted=True)
                accepted = rpc.deliver(thread_id, {'source': 'codex-monitor', 'events': [{
                    'content': 'Routing acceptance test. Reply with exactly ' + token +
                               '. Do not call tools or modify any files.'}]})
                report.update(notification_accepted=True, accepted_turn_id=accepted,
                              model_turn_submitted=True,
                              delivery_path='CodexRPC.deliver (no watcher worker)')
                deadline = time.monotonic() + args.timeout
                while time.monotonic() < deadline:
                    drain(descriptor)
                    history = rpc.request('thread/items/list', {
                        'threadId': thread_id, 'turnId': accepted,
                        'limit': 100, 'sortDirection': 'desc'})
                    matched = find_agent_ack(history.get('data', []), accepted, token)
                    if matched:
                        report.update(model_ack_verified=True, ack_token=token,
                                      matched_agent_item_id=matched['item']['id'],
                                      matched_agent_turn_id=matched['turnId'])
                        break
                    time.sleep(0.2)
                if not report['model_ack_verified']:
                    raise RuntimeError('Notification accepted, but no canonical agent acknowledgement '
                                       'was verified before the deadline')
            report['stage'] = 'passed'
    except Exception as exc:
        report.update(error=str(exc), passed=False)
        if thread_id is not None:
            snapshot(rpc, thread_id, report)
    finally:
        if pid is not None:
            try:
                report['child_exit_code'] = stop_child(pid, descriptor)
            except Exception as exc:
                report['child_cleanup_error'] = str(exc)
                report['passed'] = False
        if descriptor is not None:
            os.close(descriptor)
        if group_id is not None:
            # Clean up a shim child even if its PTY-group leader exited first.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(group_id, signal.SIGKILL)
        if thread_id is not None:
            try:
                rpc.request('thread/archive', {'threadId': thread_id}, mutating=True)
                report['test_thread_archived'] = True
            except Exception as exc:
                report['thread_cleanup_error'] = str(exc)
                report['passed'] = False
        rpc.close()
        temporary.cleanup()
    if args.output:
        save(args.output, report)
    print(json.dumps(report, indent=2))
    return 0 if report['passed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Explicitly run the real CLI fixture check')
    parser.add_argument('--deliver', action='store_true',
                        help='Warm up via a real user prompt, then submit one notification and verify both replies')
    parser.add_argument('--codex-bin')
    parser.add_argument('--codex-home')
    parser.add_argument('--socket')
    parser.add_argument('--cwd', help='Optional existing trusted workspace; no trust settings are modified')
    parser.add_argument('--timeout', type=float, default=60,
                        help='Per-stage timeout in seconds, between 5 and 300')
    parser.add_argument('--output')
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--child-thread', help=argparse.SUPPRESS)
    parser.add_argument('--child-state', help=argparse.SUPPRESS)
    parser.add_argument('--child-report', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        return child(args)
    if not args.run:
        parser.error('--run is required: this creates and archives an owned real test thread')
    return run(args)


if __name__ == '__main__':
    raise SystemExit(main())
