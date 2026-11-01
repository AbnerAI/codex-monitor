import copy
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from codex_monitor.launch import (Decoration, clear_terminal_variable,
                                  interactive_arguments, launch, wait_child)
from codex_monitor.launch_state import (create_lease, invalidate_lease, launch_lock,
                                       read_lease)


SESSION = '11111111-2222-4333-8444-555555555555'
TOKEN = '0123456789abcdef0123456789abcdef'
NEW_TOKEN = 'abcdef0123456789abcdef0123456789'
TTY = '/dev/test-codex-terminal'
DESCRIPTOR = 73
EXPRESSION = r'\(user.codexMonitor)'


class LaunchSelection(unittest.TestCase):
    def test_local_interactive_modes_preserve_option_values_and_literal_prompt_boundary(self):
        for arguments in ([], ['a prompt'], ['resume', '--last'], ['fork', 'thread-id'],
                          ['-m', 'exec', '-c', 'features.foo=true', 'resume'],
                          ['--model=review', 'a prompt'], ['--', 'exec'],
                          ['--', '--help'], ['-C', 'exec', 'a prompt']):
            with self.subTest(arguments=arguments):
                self.assertTrue(interactive_arguments(arguments))

    def test_noninteractive_and_unsupported_modes_never_get_terminal_decoration(self):
        for arguments in (['exec', 'a prompt'], ['e', 'a prompt'], ['review'],
                          ['--model', 'exec', 'exec', 'a prompt'], ['app-server'],
                          ['-c', 'value=1', 'mcp', 'list'], ['--version'],
                          ['resume', '--help'], ['--no-daemon'],
                          ['--remote', 'unix:///tmp/server'], ['--remote=ws://host']):
            with self.subTest(arguments=arguments):
                self.assertFalse(interactive_arguments(arguments))


class TerminalDecoration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cm-decoration-')
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.original = {
            'enabled': False,
            'layout': {
                'components': [{'class': 'ExistingClock', 'configuration': {'knobs': {'format': 'HH:mm'}}}],
                'advanced': {'font': 'Existing Font', 'separator': True},
            },
        }
        self.current = copy.deepcopy(self.original)
        self.api_calls = []

        def api(identifier, operation, properties=None):
            self.assertEqual(identifier, SESSION)
            self.api_calls.append((operation, copy.deepcopy(properties)))
            if operation == 'apply':
                if 'Status Bar Layout' in properties:
                    self.current['layout'] = copy.deepcopy(properties['Status Bar Layout'])
                if 'Show Status Bar' in properties:
                    self.current['enabled'] = properties['Show Status Bar']
            return copy.deepcopy(self.current)

        self.api = patch('codex_monitor.launch.profile_api', side_effect=api).start()
        self.action = patch('codex_monitor.launch.action', return_value={'tty': TTY}).start()
        patch('codex_monitor.launch.os.isatty', return_value=True).start()
        patch('codex_monitor.launch.os.ttyname', return_value=TTY).start()
        self.clear = patch('codex_monitor.launch.clear_terminal_variable').start()
        self.stop = patch('codex_monitor.launch.stop_owned_launch_updater').start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.temp.cleanup)

    def test_successful_lifecycle_preserves_components_and_restores_only_its_session(self):
        before = copy.deepcopy(self.original)
        decoration = Decoration(self.root, SESSION, DESCRIPTOR).prepare()
        self.assertEqual(self.original, before)
        self.assertTrue(self.current['enabled'])
        self.assertEqual(self.current['layout']['components'][:-1], before['layout']['components'])
        self.assertEqual(self.current['layout']['advanced'], before['layout']['advanced'])
        self.assertEqual(self.current['layout']['components'][-1]['configuration']['knobs']['expression'], EXPRESSION)
        self.assertTrue(read_lease(self.root, SESSION)['active'])

        decoration.close()
        self.assertEqual(self.current, before)
        self.assertFalse(read_lease(self.root, SESSION)['active'])
        self.stop.assert_called_once_with(self.root, SESSION, decoration.token)
        self.assertEqual(self.clear.call_count, 2)
        self.clear.assert_called_with(DESCRIPTOR)
        self.action.assert_called_once_with('probe', SESSION)
        self.assertEqual([name for name, _ in self.api_calls], ['get', 'apply', 'get', 'apply'])

    def test_existing_monitor_component_is_reused_and_original_visibility_restored(self):
        self.current['layout']['components'].append({
            'class': 'iTermStatusBarSwiftyStringComponent',
            'configuration': {'knobs': {'expression': EXPRESSION, 'color': '#00ff00'}},
        })
        before = copy.deepcopy(self.current)
        decoration = Decoration(self.root, SESSION, DESCRIPTOR).prepare()
        self.assertEqual(self.current['layout'], before['layout'])
        self.assertEqual(len(self.current['layout']['components']), 2)
        self.assertTrue(self.current['enabled'])
        decoration.close()
        self.assertEqual(self.current, before)

    def test_user_layout_changes_during_codex_are_preserved_on_exit(self):
        decoration = Decoration(self.root, SESSION, DESCRIPTOR).prepare()
        self.current['layout']['components'].insert(0, {'class': 'UserAddedComponent'})
        self.current['layout']['advanced']['font'] = 'New User Font'
        edited = copy.deepcopy(self.current)
        self.api.reset_mock()

        decoration.close()
        self.assertEqual(self.current, edited)
        self.assertEqual(self.api.call_count, 1)
        self.stop.assert_called_once_with(self.root, SESSION, decoration.token)
        self.clear.assert_called_with(DESCRIPTOR)

    def test_old_launcher_cannot_clear_or_restore_a_new_launcher(self):
        decoration = Decoration(self.root, SESSION, DESCRIPTOR).prepare()
        with launch_lock(self.root, SESSION):
            invalidate_lease(self.root, SESSION, decoration.token)
            create_lease(self.root, SESSION, NEW_TOKEN)
        before = copy.deepcopy(self.current)
        self.api.reset_mock()
        self.clear.reset_mock()

        decoration.close()
        self.assertEqual(self.current, before)
        self.assertTrue(read_lease(self.root, SESSION)['active'])
        self.assertEqual(read_lease(self.root, SESSION)['token'], NEW_TOKEN)
        self.stop.assert_not_called()
        self.clear.assert_not_called()
        self.api.assert_not_called()

    def test_wrong_terminal_fd_is_rejected_before_profile_or_lease_mutation(self):
        with patch('codex_monitor.launch.os.ttyname', return_value='/dev/other-terminal'):
            with self.assertRaisesRegex(ValueError, 'Terminal FD'):
                Decoration(self.root, SESSION, DESCRIPTOR).prepare()
        self.api.assert_not_called()
        self.clear.assert_not_called()
        self.assertIsNone(read_lease(self.root, SESSION))

    def test_partial_apply_failure_rolls_back_only_the_property_it_changed(self):
        original_api = self.api.side_effect
        first_apply = True

        def partial_failure(identifier, operation, properties=None):
            nonlocal first_apply
            if operation == 'apply' and first_apply:
                first_apply = False
                self.current['enabled'] = properties['Show Status Bar']
                raise RuntimeError('layout update failed after visibility changed')
            return original_api(identifier, operation, properties)

        self.api.side_effect = partial_failure
        decoration = Decoration(self.root, SESSION, DESCRIPTOR)
        with self.assertRaisesRegex(RuntimeError, 'visibility changed'):
            decoration.prepare()
        self.assertTrue(self.current['enabled'])
        decoration.close()
        self.assertEqual(self.current, self.original)
        self.assertEqual(self.api_calls[-1], ('apply', {'Show Status Bar': False}))
        self.assertFalse(read_lease(self.root, SESSION)['active'])


class LaunchRunner(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cm-launch-runner-')
        self.root = Path(self.temp.name)
        self.original = str(self.root / 'existing codex wrapper')
        self.environment = {'TERM_PROGRAM': 'iTerm.app', 'ITERM_SESSION_ID': 'w0t0p0:' + SESSION,
                            'CODEX_HOME': str(self.root / 'parent-profile')}
        patch.dict(os.environ, self.environment, clear=True).start()
        patch('codex_monitor.launch.sys.platform', 'darwin').start()
        patch('codex_monitor.launch.os.isatty', return_value=True).start()
        patch('codex_monitor.launch.os.ttyname', return_value=TTY).start()
        self.duplicate = patch('codex_monitor.launch.os.dup', return_value=DESCRIPTOR).start()
        self.close_fd = patch('codex_monitor.launch.os.close').start()
        self.decoration_class = patch('codex_monitor.launch.Decoration').start()
        self.decoration = self.decoration_class.return_value
        self.decoration.identifier, self.decoration.token = SESSION, TOKEN
        self.wait = patch('codex_monitor.launch.wait_child', return_value=7).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.temp.cleanup)

    def test_api_unavailable_still_launches_original_once_with_untouched_arguments(self):
        arguments = ['--model', 'my model', '--', 'a prompt with spaces and "quotes"']
        before = list(arguments)
        self.decoration.prepare.side_effect = RuntimeError('Python API is disabled')
        with patch('sys.stderr', new_callable=io.StringIO) as error:
            result = launch(self.root, self.original, arguments)
        self.assertEqual(result, 7)
        self.assertEqual(arguments, before)
        self.wait.assert_called_once_with([self.original] + before, self.environment, None)
        self.decoration.close.assert_called_once()
        self.close_fd.assert_called_once_with(DESCRIPTOR)
        self.assertIn('indicator unavailable', error.getvalue())

    def test_success_adds_only_exact_session_environment_keys_and_retains_all_user_arguments(self):
        arguments = ['--model', 'my model', '--sandbox', 'workspace-write', '--', 'literal prompt']
        result = launch(self.root, self.original, ['--'] + arguments)
        self.assertEqual(result, 7)
        self.wait.assert_called_once()
        command, environment, decoration = self.wait.call_args.args
        self.assertEqual(command[0], self.original)
        self.assertEqual(command[-len(arguments):], arguments)
        overrides = command[1:-len(arguments)]
        self.assertEqual(overrides[::2], ['-c', '-c', '-c'])
        values = {key: json.loads(value) for key, value in (item.split('=', 1) for item in overrides[1::2])}
        self.assertEqual(values, {
            'shell_environment_policy.set.CODEX_MONITOR_LAUNCH_TOKEN': TOKEN,
            'shell_environment_policy.set.CODEX_MONITOR_ITERM_SESSION': SESSION,
            'shell_environment_policy.set.CODEX_MONITOR_HOME': str(self.root),
        })
        self.assertEqual(environment, dict(self.environment, CODEX_MONITOR_LAUNCH_ACTIVE=TOKEN))
        self.assertIs(decoration, self.decoration)
        self.decoration.close.assert_called_once()
        self.close_fd.assert_called_once_with(DESCRIPTOR)

    def test_skipped_modes_do_not_duplicate_terminal_or_construct_decoration(self):
        scenarios = [({'TERM_PROGRAM': 'Apple_Terminal'}, []),
                     ({'CODEX_MONITOR_LAUNCH_ACTIVE': 'parent-token'}, []),
                     ({}, ['exec', 'a prompt']), ({}, ['--version'])]
        for changes, arguments in scenarios:
            with self.subTest(changes=changes, arguments=arguments), patch.dict(os.environ, changes):
                result = launch(self.root, self.original, arguments)
                self.assertEqual(result, 7)
        self.assertEqual(self.wait.call_count, len(scenarios))
        self.duplicate.assert_not_called()
        self.decoration_class.assert_not_called()

    def test_redirected_or_different_terminals_leave_original_launch_unchanged(self):
        with patch('codex_monitor.launch.os.isatty', side_effect=lambda fd: fd != 1):
            self.assertEqual(launch(self.root, self.original, []), 7)
        self.duplicate.assert_not_called()
        with patch('codex_monitor.launch.os.ttyname', side_effect=lambda fd: TTY if fd == 0 else '/dev/other'):
            self.assertEqual(launch(self.root, self.original, []), 7)
        self.decoration_class.assert_not_called()
        self.close_fd.assert_called_once_with(DESCRIPTOR)
        self.assertEqual(self.wait.call_count, 2)
        for call in self.wait.call_args_list:
            self.assertEqual(call.args[:2], ([self.original], self.environment))

    def test_cleanup_runs_even_when_starting_the_original_child_fails(self):
        self.wait.side_effect = OSError('child executable disappeared')
        with self.assertRaisesRegex(OSError, 'disappeared'):
            launch(self.root, self.original, [])
        self.wait.assert_called_once()
        self.decoration.close.assert_called_once()
        self.close_fd.assert_called_once_with(DESCRIPTOR)


class ChildLifecycle(unittest.TestCase):
    def test_exit_status_and_signal_handlers_are_preserved_for_a_normal_child(self):
        previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        process = Mock()
        process.wait.return_value = 23
        with patch('codex_monitor.launch.subprocess.Popen', return_value=process) as popen:
            self.assertEqual(wait_child(['/original/codex', 'prompt'], {'UNCHANGED': 'yes'}), 23)
        popen.assert_called_once_with(['/original/codex', 'prompt'], env={'UNCHANGED': 'yes'})
        self.assertEqual({number: signal.getsignal(number) for number in previous}, previous)

    def test_signals_wait_for_child_exit_before_returning_a_shell_exit_status(self):
        for number, status in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(signal=number):
                previous = {item: signal.getsignal(item) for item in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
                handlers = dict(previous)
                process = Mock()
                process.poll.return_value = None
                decoration = Mock()
                attempts = []

                def set_handler(item, handler):
                    old, handlers[item] = handlers[item], handler
                    return old

                def wait(timeout):
                    attempts.append(timeout)
                    if len(attempts) == 1:
                        handlers[number](number, None)
                        raise subprocess.TimeoutExpired('codex', timeout)
                    return -number

                process.wait.side_effect = wait
                with patch('codex_monitor.launch.signal.signal', side_effect=set_handler), \
                     patch('codex_monitor.launch.subprocess.Popen', return_value=process):
                    self.assertEqual(wait_child(['/original/codex'], {}, decoration), status)
                self.assertEqual(len(attempts), 2)
                decoration.heartbeat.assert_called_once()
                decoration.close.assert_not_called()
                self.assertEqual(handlers, previous)
                if number == signal.SIGINT:
                    process.send_signal.assert_not_called()
                else:
                    process.send_signal.assert_called_once_with(number)

    def test_failed_spawn_restores_parent_signal_handlers(self):
        previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        with patch('codex_monitor.launch.subprocess.Popen', side_effect=OSError('cannot spawn')):
            with self.assertRaisesRegex(OSError, 'cannot spawn'):
                wait_child(['/original/codex'], {})
        self.assertEqual({number: signal.getsignal(number) for number in previous}, previous)


class TerminalVariableWrites(unittest.TestCase):
    def test_clear_survives_eintr_and_partial_writes_without_losing_bytes(self):
        writes = []
        chunks = iter([InterruptedError(), 2, 3, 1000])

        def write(descriptor, data):
            self.assertEqual(descriptor, DESCRIPTOR)
            item = next(chunks)
            if isinstance(item, Exception):
                raise item
            count = min(item, len(data))
            writes.append(data[:count])
            return count

        with patch('codex_monitor.launch.os.write', side_effect=write):
            clear_terminal_variable(DESCRIPTOR)
        self.assertEqual(b''.join(writes), b'\x1b]1337;SetUserVar=codexMonitor=\x07')

    def test_zero_progress_is_an_error_instead_of_an_infinite_cleanup_loop(self):
        with patch('codex_monitor.launch.os.write', return_value=0) as write:
            with self.assertRaisesRegex(OSError, 'no progress'):
                clear_terminal_variable(DESCRIPTOR)
        write.assert_called_once()


if __name__ == '__main__':
    unittest.main()
