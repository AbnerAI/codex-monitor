import io
import json
import signal
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from codex_monitor import client_home as home


class ClientHome(unittest.TestCase):
    def test_original_entry_and_only_config_profile_context_are_preserved(self):
        args = ['--model', 'exec', '-c', 'x="a b"', '--profile=official',
                'resume', '--last', '--', '-c', 'ignored=1']
        with patch.object(home, '_version', return_value={'codexHome': '/profiles/a/../chosen'}) as version, \
             patch.object(home, '_stdio_home') as stdio:
            result = home.discover_client_home('/original account wrapper', args)
        self.assertEqual(result, str(Path('/profiles/chosen').resolve()))
        self.assertEqual(version.call_args.args[0],
                         ['/original account wrapper', '-c', 'x="a b"', '--profile=official'])
        stdio.assert_not_called()

    def test_existing_daemon_endpoint_is_used_without_inferring_home_from_its_path(self):
        with patch.object(home, '_version', return_value={'socketPath': '/unrelated/control.sock'}), \
             patch.object(home, '_socket_home', return_value='/authoritative/home') as socket, \
             patch.object(home, '_stdio_home') as stdio:
            self.assertEqual(home.discover_client_home('/wrapper', []), '/authoritative/home')
        self.assertEqual(socket.call_args.args[0], '/unrelated/control.sock')
        stdio.assert_not_called()

    def test_missing_daemon_uses_temporary_initialize_with_same_original_entry(self):
        with patch.object(home, '_version', return_value=None), \
             patch.object(home, '_stdio_home', return_value='/selected/home') as stdio:
            self.assertEqual(home.discover_client_home('/wrapper', ['-p', 'work']), '/selected/home')
        self.assertEqual(stdio.call_args.args[0], ['/wrapper', '-p', 'work'])

    def test_failed_daemon_handshake_falls_back_without_exposing_child_errors(self):
        with patch.object(home, '_version', return_value={'socketPath': '/socket'}), \
             patch.object(home, '_socket_home', side_effect=OSError('private-cookie')), \
             patch.object(home, '_stdio_home', side_effect=ValueError('private-cookie')):
            with self.assertRaisesRegex(RuntimeError, 'original Codex client home') as error:
                home.discover_client_home('/wrapper', [])
        self.assertNotIn('private-cookie', str(error.exception))

    def test_stdio_keeps_input_open_until_reply_and_sends_initialize_only(self):
        process = SimpleNamespace(stdin=io.BytesIO(), stdout=Mock(), pid=731, poll=Mock(return_value=None),
                                  wait=Mock(return_value=0))
        process.stdout.fileno.return_value = 88
        response = json.dumps({'id': home._REQUEST['id'], 'result': {'codexHome': '/chosen/home'}}).encode() + b'\n'
        written = []

        def read(fd, size):
            self.assertFalse(process.stdin.closed)
            written.append(process.stdin.getvalue())
            return response

        with patch.object(home, '_spawn', return_value=process) as spawn, \
             patch.object(home.select, 'select', return_value=([process.stdout], [], [])), \
             patch.object(home.os, 'read', side_effect=read), patch.object(home.os, 'killpg') as kill:
            result = home._stdio_home(['/original', '-c', 'x=1'], home.time.monotonic() + 1)
        self.assertEqual(result, str(Path('/chosen/home').resolve()))
        spawn.assert_called_once_with(['/original', '-c', 'x=1', 'app-server'])
        self.assertEqual([json.loads(line)['method'] for line in written[0].splitlines()], ['initialize'])
        self.assertTrue(process.stdin.closed)
        kill.assert_called_once_with(process.pid, signal.SIGKILL)

    def test_stdio_timeout_stops_only_its_new_process_group(self):
        process = SimpleNamespace(stdin=io.BytesIO(), stdout=Mock(), pid=732, poll=Mock(return_value=None),
                                  wait=Mock(return_value=0))
        with patch.object(home, '_spawn', return_value=process), \
             patch.object(home.select, 'select', return_value=([], [], [])), \
             patch.object(home.os, 'killpg') as kill:
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                home._stdio_home(['/original'], home.time.monotonic() + 1)
        kill.assert_called_once_with(732, signal.SIGKILL)
        self.assertTrue(process.stdin.closed)

    def test_spawn_has_separate_process_group_and_no_shell(self):
        with patch.object(home.subprocess, 'Popen') as spawn:
            home._spawn(['/wrapper', 'app-server'])
        spawn.assert_called_once_with(['/wrapper', 'app-server'], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)

    def test_exited_wrapper_still_cleans_up_its_spawned_cli_process_group(self):
        process = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(), pid=733,
                                  poll=Mock(return_value=0), wait=Mock(return_value=0))
        with patch.object(home.os, 'killpg') as kill:
            home._stop(process)
        kill.assert_called_once_with(733, signal.SIGKILL)
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)

    def test_invalid_home_or_timeout_never_returns_an_inferred_profile(self):
        for value in (None, '', 'relative/home', '/home\0bad'):
            with self.subTest(value=value), patch.object(home, '_version', return_value={'codexHome': value}):
                with self.assertRaises(RuntimeError):
                    home.discover_client_home('/wrapper', [])
        for timeout in (0, -1, 6, float('nan'), True):
            with self.subTest(timeout=timeout), patch.object(home, '_spawn') as spawn:
                with self.assertRaises(ValueError):
                    home.discover_client_home('/wrapper', [], timeout)
                spawn.assert_not_called()
