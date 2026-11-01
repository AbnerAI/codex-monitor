import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from codex_monitor.client_counter import TITLE_ITEMS, native_thread_prefix
from codex_monitor.iterm import SessionMismatchError, attach_for_launch, attachment_path
from codex_monitor.launch import Decoration, launch_session, wait_child
from codex_monitor.launch_state import (create_lease, invalidate_lease, launch_lock,
                                       lease_path, read_lease)


SESSION_A = '11111111-2222-4333-8444-555555555555'
SESSION_B = 'AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE'
THREAD_A = '550e8400-e29b-41d4-a716-446655440000'
THREAD_B = '12345678-1234-1234-1234-123456789abc'
PREFIX_A = '550e8400-e29b-41d4-a716-44665'
TITLE_A = 'codex | ' + PREFIX_A + '... | codex | Fix login timeout | project'
TITLE_B = 'codex | ' + THREAD_B[:29] + '... | codex | resumed task | project'
NEW_TOKEN = 'abcdef0123456789abcdef0123456789'


class LaunchTerminalOrigin(unittest.TestCase):
    def test_real_launch_fd_selects_its_terminal_despite_stale_inherited_identifiers(self):
        stale = {'ITERM_SESSION_ID': 'w0t0p0:' + SESSION_A,
                 'CODEX_MONITOR_ITERM_SESSION': SESSION_A,
                 'CODEX_MONITOR_LAUNCH_TOKEN': NEW_TOKEN}
        with patch.dict(os.environ, stale), \
             patch('codex_monitor.launch.action', return_value={
                 'session_id': SESSION_B, 'tty': '/dev/client-b', 'panes': 1}) as action, \
             patch('codex_monitor.launch.os.isatty', return_value=True), \
             patch('codex_monitor.launch.os.ttyname', return_value='/dev/client-b'):
            self.assertEqual(launch_session(72), SESSION_B)
        action.assert_called_once_with('identify', None)

    def test_focus_on_a_different_terminal_or_multiple_panes_is_not_launch_ownership(self):
        for observed, real_tty, is_tty in (({'tty': '/dev/other', 'panes': 1}, '/dev/client', True),
                                           ({'tty': '/dev/client', 'panes': 2}, '/dev/client', True),
                                           ({'tty': '/dev/client', 'panes': 1}, '/dev/client', False)):
            with self.subTest(observed=observed, is_tty=is_tty), \
                 patch('codex_monitor.launch.action', return_value=dict(observed, session_id=SESSION_B)), \
                 patch('codex_monitor.launch.os.isatty', return_value=is_tty), \
                 patch('codex_monitor.launch.os.ttyname', return_value=real_tty):
                with self.assertRaises(ValueError):
                    launch_session(72)


class IndependentClientCounters(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cm-native-client-')
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.home = str(self.root / 'existing-profile')
        self.other_home = str(self.root / 'other-profile')
        self.current_gui = SESSION_A
        self.fd_session = {71: SESSION_A, 72: SESSION_B}
        self.tty = {SESSION_A: '/dev/client-a', SESSION_B: '/dev/client-b'}
        self.titles = {SESSION_A: TITLE_A, SESSION_B: TITLE_A}
        self.profiles = {identifier: {'enabled': False, 'layout': {
            'components': [{'class': 'ExistingClock'}], 'advanced': {'font': 'UserFont'}}}
            for identifier in (SESSION_A, SESSION_B)}
        self.rows = [{'active': True, 'thread_id': THREAD_A, 'codex_home': self.home},
                     {'active': True, 'thread_id': THREAD_B, 'codex_home': self.home}]
        self.api_calls = []

        def api(identifier, operation, properties=None):
            self.assertIn(identifier, self.profiles)
            self.api_calls.append((identifier, operation, copy.deepcopy(properties)))
            if operation == 'identity':
                return {'native_title': self.titles[identifier]}
            if operation == 'apply':
                for key, field in (('Status Bar Layout', 'layout'), ('Show Status Bar', 'enabled')):
                    if key in properties:
                        self.profiles[identifier][field] = copy.deepcopy(properties[key])
            return copy.deepcopy(self.profiles[identifier])

        def focused(operation, identifier, payload=None):
            if identifier != self.current_gui:
                raise SessionMismatchError(identifier, self.current_gui)
            return {'session_id': identifier, 'tty': self.tty[identifier], 'panes': 1}

        def title_write(descriptor, title):
            self.titles[self.fd_session[descriptor]] = title

        def registry(root, request, **kwargs):
            self.assertEqual(Path(root), self.root)
            self.assertEqual(request, {'method': 'list'})
            return copy.deepcopy(self.rows)

        self.api = patch('codex_monitor.launch.profile_api', side_effect=api).start()
        self.gui = patch('codex_monitor.launch.action', side_effect=focused).start()
        patch('codex_monitor.launch.os.isatty', return_value=True).start()
        patch('codex_monitor.launch.os.ttyname', side_effect=lambda fd: self.tty[self.fd_session[fd]]).start()
        self.title_write = patch('codex_monitor.launch._write_terminal_title', side_effect=title_write).start()
        self.clear = patch('codex_monitor.launch.clear_terminal_variable').start()
        self.publish = patch('codex_monitor.launch.publish_terminal_variable').start()
        self.control = patch('codex_monitor.launch.control', side_effect=registry).start()
        self.stop = patch('codex_monitor.launch.stop_owned_launch_updater').start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.temp.cleanup)

    def prepare(self, identifier=SESSION_A, descriptor=71):
        self.current_gui = identifier
        return Decoration(self.root, identifier, descriptor, client_counter=True,
                          codex_home=self.home).prepare()

    def test_native_fixture_is_the_configured_0166_identity_field_not_a_searched_task_name(self):
        self.assertEqual(TITLE_ITEMS[:3], ['app-name', 'thread-id', 'app-name'])
        self.assertEqual(native_thread_prefix(TITLE_A), PREFIX_A)
        decoration = self.prepare()
        self.assertEqual(read_lease(self.root, SESSION_A)['counter_manager'], 'launch_client')
        self.assertNotEqual(self.titles[SESSION_A], TITLE_A)
        self.assertIsNone(native_thread_prefix(self.titles[SESSION_A]))
        decoration.update_counter()
        self.publish.assert_not_called()
        self.assertEqual(read_lease(self.root, SESSION_A)['counter']['state'], 'awaiting_client_identity')
        self.control.assert_called_once()

    def test_two_new_clients_count_existing_registry_without_registering_new_watchers(self):
        first = self.prepare()
        stale = {'ITERM_SESSION_ID': SESSION_A, 'CODEX_MONITOR_ITERM_SESSION': SESSION_A,
                 'CODEX_MONITOR_LAUNCH_TOKEN': first.token, 'CODEX_THREAD_ID': THREAD_A,
                 'CODEX_HOME': self.other_home}
        with patch.dict(os.environ, stale):
            second = self.prepare(SESSION_B, 72)
            self.titles.update({SESSION_A: TITLE_A, SESSION_B: TITLE_B})
            self.gui.reset_mock()
            self.api_calls.clear()
            first.update_counter()
            second.update_counter()
        self.assertEqual(self.publish.call_args_list, [call(71, '1 monitor'), call(72, '1 monitor')])
        self.gui.assert_not_called()
        self.assertEqual({identifier for identifier, operation, _ in self.api_calls if operation == 'identity'},
                         {SESSION_A, SESSION_B})
        self.assertEqual(read_lease(self.root, SESSION_A)['counter']['thread_id'], THREAD_A)
        self.assertEqual(read_lease(self.root, SESSION_B)['counter']['thread_id'], THREAD_B)
        self.assertEqual(read_lease(self.root, SESSION_B)['counter']['codex_home'], self.home)
        # Both can also resume the same thread; the existing single watcher is
        # counted separately by each live client, without another registration.
        self.titles[SESSION_B] = TITLE_A
        second.update_counter()
        self.assertEqual(read_lease(self.root, SESSION_B)['counter']['thread_id'], THREAD_A)
        self.assertEqual(read_lease(self.root, SESSION_B)['counter']['active'], 1)
        self.assertTrue(all(item.args[1] == {'method': 'list'} for item in self.control.call_args_list))

    def test_thread_switch_updates_scope_and_ambiguous_or_missing_identity_clears_the_label(self):
        decoration = self.prepare()
        self.titles[SESSION_A] = TITLE_A
        decoration.update_counter()
        self.titles[SESSION_A] = TITLE_B
        decoration.update_counter()
        self.assertEqual(read_lease(self.root, SESSION_A)['counter']['thread_id'], THREAD_B)
        self.rows.append({'active': True, 'thread_id': THREAD_B[:29] + 'fffffff',
                          'codex_home': self.home})
        decoration.update_counter()
        counter = read_lease(self.root, SESSION_A)['counter']
        self.assertEqual(counter['state'], 'ambiguous_client_identity')
        self.assertIsNone(counter['thread_id'])
        self.assertEqual(self.publish.call_args_list[-1], call(71, ''))
        self.rows.pop()
        decoration.update_counter()
        self.titles[SESSION_A] = 'codex | codex | ' + THREAD_A + ' | UUID-shaped task name'
        decoration.update_counter()
        self.assertEqual(read_lease(self.root, SESSION_A)['counter']['state'], 'awaiting_client_identity')
        self.assertEqual(self.publish.call_args_list[-1], call(71, ''))

    def test_watchers_in_another_profile_cannot_supply_the_current_clients_count(self):
        decoration = self.prepare()
        self.titles[SESSION_A] = TITLE_A
        self.rows = [{'active': True, 'thread_id': THREAD_A, 'codex_home': self.other_home}]
        decoration.update_counter()
        counter = read_lease(self.root, SESSION_A)['counter']
        self.assertEqual(counter['state'], 'no_active_watchers')
        self.assertEqual(counter['label'], '')
        self.assertIsNone(counter['codex_home'])
        self.publish.assert_not_called()

    def test_missing_authoritative_client_home_fails_before_terminal_mutations(self):
        self.api.reset_mock()
        self.gui.reset_mock()
        with self.assertRaisesRegex(ValueError, 'profile'):
            Decoration(self.root, SESSION_A, 71, client_counter=True).prepare()
        self.api.assert_not_called()
        self.gui.assert_not_called()
        self.clear.assert_not_called()

    def test_metadata_failure_clears_previous_value_and_never_uses_another_focused_session(self):
        decoration = self.prepare()
        self.titles[SESSION_A] = TITLE_A
        decoration.update_counter()
        self.current_gui = SESSION_B
        self.gui.reset_mock()
        self.api.side_effect = RuntimeError('target metadata temporarily unavailable')
        decoration.update_counter()
        self.gui.assert_not_called()
        self.api.assert_called_with(SESSION_A, 'identity')
        self.assertEqual(self.publish.call_args_list[-1], call(71, ''))
        self.assertEqual(read_lease(self.root, SESSION_A)['counter']['state'], 'counter_unavailable')

    def test_old_tick_cannot_publish_or_overwrite_a_newer_launch_owner(self):
        decoration = self.prepare()
        with launch_lock(self.root, SESSION_A):
            invalidate_lease(self.root, SESSION_A, decoration.token)
            create_lease(self.root, SESSION_A, NEW_TOKEN)
        before = lease_path(self.root, SESSION_A).read_bytes()
        self.publish.reset_mock()
        self.titles[SESSION_A] = TITLE_A
        decoration.update_counter()
        self.publish.assert_not_called()
        self.assertEqual(lease_path(self.root, SESSION_A).read_bytes(), before)

    def test_watch_time_registration_does_not_start_a_second_gui_updater_for_launch_client(self):
        decoration = self.prepare()
        markers = {'CODEX_MONITOR_ITERM_SESSION': SESSION_A,
                   'CODEX_MONITOR_LAUNCH_TOKEN': decoration.token, 'ITERM_SESSION_ID': SESSION_B}
        with patch.dict(os.environ, markers), \
             patch('codex_monitor.iterm.attach') as attach, \
             patch('codex_monitor.iterm.detach') as detach, \
             patch('codex_monitor.iterm.action') as action:
            pending = attach_for_launch(self.root, THREAD_A, self.home)
            self.assertFalse(pending['attached'])
            self.assertEqual(pending['managed_by'], 'launch_client')
            self.titles[SESSION_A] = TITLE_A
            decoration.update_counter()
            bound = attach_for_launch(self.root, THREAD_A, self.home)
            self.assertTrue(bound['attached'])
            self.assertEqual(bound['managed_by'], 'launch_client')
            attach.assert_not_called()
            detach.assert_not_called()
            action.assert_not_called()
        self.assertFalse(attachment_path(self.root, SESSION_A).exists())

    def test_previous_clients_title_is_not_accepted_until_starting_epoch_has_been_acknowledged(self):
        previous_api = self.api.side_effect
        pending = {}
        remaining = [2]

        def delayed_write(descriptor, title):
            pending['identifier'], pending['title'] = self.fd_session[descriptor], title

        def delayed_metadata(identifier, operation, properties=None):
            if operation == 'identity' and pending:
                remaining[0] -= 1
                if remaining[0] <= 0:
                    self.titles[pending['identifier']] = pending.pop('title')
                    pending.pop('identifier')
            return previous_api(identifier, operation, properties)

        self.title_write.side_effect = delayed_write
        self.api.side_effect = delayed_metadata
        decoration = self.prepare()
        # A completed prepare must have established its own epoch. Otherwise
        # the first tick could bind watchers from the previous client's title.
        self.assertEqual(self.titles[SESSION_A], decoration.starting_title)
        self.assertFalse(pending)
        decoration.update_counter()
        self.publish.assert_not_called()
        self.assertIsNone(read_lease(self.root, SESSION_A)['counter']['thread_id'])


class ClientCounterLoop(unittest.TestCase):
    def test_child_timeout_refreshes_ownership_and_updates_counter_without_new_watch(self):
        process = Mock()
        process.wait.side_effect = [subprocess.TimeoutExpired('codex', 1), 17]
        decoration = Mock()
        with patch('codex_monitor.launch.subprocess.Popen', return_value=process):
            self.assertEqual(wait_child(['/existing/codex'], {}, decoration), 17)
        self.assertEqual(decoration.method_calls, [call.heartbeat(), call.update_counter()])


if __name__ == '__main__':
    unittest.main()
