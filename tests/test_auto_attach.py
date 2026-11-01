from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from codex_monitor.iterm import (attach_for_launch, attachment_path, save,
                                stop_owned_launch_updater)
from codex_monitor.launch_state import create_lease, invalidate_lease, launch_lock


SESSION = '11111111-2222-4333-8444-555555555555'
TOKEN = '0123456789abcdef0123456789abcdef'
NEW_TOKEN = 'abcdef0123456789abcdef0123456789'


class AutomaticLaunchBinding(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cm-auto-attach-')
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.home = str(self.root / 'actual-selected-profile')
        self.env = {'CODEX_MONITOR_ITERM_SESSION': 'w0t0p0:' + SESSION,
                    'CODEX_MONITOR_LAUNCH_TOKEN': TOKEN,
                    'CODEX_HOME': str(self.root / 'unrelated-parent-profile')}
        with launch_lock(self.root, SESSION):
            create_lease(self.root, SESSION, TOKEN)

    def tearDown(self):
        self.temp.cleanup()

    def attachment(self, thread='actual-thread', home=None, token=TOKEN, **changes):
        plan = {'active': True, 'updated_at': time.time(), 'thread_id': thread,
                'codex_home': home or self.home, 'launch_token': token,
                'session_id': SESSION}
        plan.update(changes)
        path = attachment_path(self.root, SESSION)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        save(path, plan)
        return plan

    def test_no_launch_markers_means_no_terminal_or_service_operations(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch('codex_monitor.iterm.action') as action, \
             patch('codex_monitor.iterm.attach') as attach, \
             patch('codex_monitor.iterm.detach') as detach, \
             patch('codex_monitor.iterm.control') as control:
            self.assertIsNone(attach_for_launch(self.root, 'actual-thread', self.home))
        action.assert_not_called()
        attach.assert_not_called()
        detach.assert_not_called()
        control.assert_not_called()

    def test_revoked_or_replaced_launch_cannot_attach_or_touch_the_gui(self):
        with launch_lock(self.root, SESSION):
            invalidate_lease(self.root, SESSION, TOKEN)
        for owner in ('revoked', 'replaced'):
            if owner == 'replaced':
                with launch_lock(self.root, SESSION):
                    create_lease(self.root, SESSION, NEW_TOKEN)
            with self.subTest(owner=owner), \
                 patch.dict(os.environ, self.env), \
                 patch('codex_monitor.iterm.action') as action, \
                 patch('codex_monitor.iterm.attach') as attach, \
                 patch('codex_monitor.iterm.detach') as detach:
                result = attach_for_launch(self.root, 'actual-thread', self.home)
                self.assertFalse(result['attached'])
                action.assert_not_called()
                attach.assert_not_called()
                detach.assert_not_called()

    def test_concurrent_watch_registrations_share_one_updater(self):
        participants = 6
        barrier = threading.Barrier(participants)
        calls = []
        calls_lock = threading.Lock()

        def attach_once(root, args):
            with calls_lock:
                calls.append((root, args.session, args.thread, args.codex_home, args.launch_token))
            # Keep the initial binding in progress while the other callers arrive.
            time.sleep(0.04)
            self.attachment(thread=args.thread, home=args.codex_home, token=args.launch_token)
            return {'attached': True}

        def request():
            barrier.wait(timeout=5)
            return attach_for_launch(self.root, 'actual-thread', self.home)

        with patch.dict(os.environ, self.env), \
             patch('codex_monitor.iterm.attach', side_effect=attach_once), \
             patch('codex_monitor.iterm.detach') as detach, \
             patch('codex_monitor.iterm.action') as action:
            with ThreadPoolExecutor(max_workers=participants) as executor:
                results = list(executor.map(lambda _: request(), range(participants)))
        self.assertEqual(calls, [(self.root, SESSION, 'actual-thread', self.home, TOKEN)])
        self.assertTrue(all(result['attached'] for result in results))
        self.assertEqual(sum(bool(result.get('already_attached')) for result in results), participants - 1)
        action.assert_not_called()
        detach.assert_not_called()

    def test_thread_and_actual_selected_profile_determine_rebinding(self):
        for old_thread, old_home in (('previous-thread', self.home),
                                     ('actual-thread', self.env['CODEX_HOME'])):
            with self.subTest(old_thread=old_thread, old_home=old_home):
                self.attachment(thread=old_thread, home=old_home)

                def detach_old(root, identifier):
                    self.assertEqual((root, identifier), (self.root, SESSION))
                    attachment_path(root, identifier).unlink()

                with patch.dict(os.environ, self.env), \
                     patch('codex_monitor.iterm.detach', side_effect=detach_old) as detach, \
                     patch('codex_monitor.iterm.attach', return_value={'attached': True}) as attach, \
                     patch('codex_monitor.iterm.action') as action:
                    self.assertTrue(attach_for_launch(self.root, 'actual-thread', self.home)['attached'])
                detach.assert_called_once_with(self.root, SESSION)
                args = attach.call_args.args[1]
                self.assertEqual((args.session, args.thread, args.codex_home, args.launch_token),
                                 (SESSION, 'actual-thread', self.home, TOKEN))
                action.assert_not_called()

    def test_expired_updater_is_replaced_even_for_the_same_thread(self):
        self.attachment(updated_at=time.time() - 20)
        with patch.dict(os.environ, self.env), \
             patch('codex_monitor.iterm.detach') as detach, \
             patch('codex_monitor.iterm.attach', return_value={'attached': True}) as attach:
            result = attach_for_launch(self.root, 'actual-thread', self.home)
        self.assertTrue(result['attached'])
        detach.assert_called_once_with(self.root, SESSION)
        self.assertEqual(attach.call_args.args[1].codex_home, self.home)

    def test_old_cleanup_does_not_stop_a_new_owner_or_clear_its_variable(self):
        original = self.attachment(token=NEW_TOKEN)
        path = attachment_path(self.root, SESSION)
        with patch('codex_monitor.iterm.stop_updater') as stop, \
             patch('codex_monitor.iterm.action') as action, \
             patch('codex_monitor.iterm.control') as control:
            stop_owned_launch_updater(self.root, SESSION, TOKEN)
        stop.assert_not_called()
        action.assert_not_called()
        control.assert_not_called()
        self.assertEqual(json.loads(path.read_text()), original)
        self.assertFalse(path.with_suffix('.stop').exists())
        self.assertFalse(path.with_suffix('.detached.json').exists())

    def test_cleanup_stops_only_its_indicator_without_stopping_watchers(self):
        self.attachment()
        path = attachment_path(self.root, SESSION)
        path.with_suffix('.stop').touch()
        with launch_lock(self.root, SESSION):
            self.assertTrue(invalidate_lease(self.root, SESSION, TOKEN))
            with patch('codex_monitor.iterm.stop_updater') as stop, \
                 patch('codex_monitor.iterm.action') as action, \
                 patch('codex_monitor.iterm.control') as control:
                stop_owned_launch_updater(self.root, SESSION, TOKEN)
        stop.assert_called_once_with(path)
        action.assert_not_called()
        control.assert_not_called()
        self.assertFalse(path.exists())
        self.assertFalse(path.with_suffix('.stop').exists())
        self.assertEqual(json.loads(path.with_suffix('.detached.json').read_text())['launch_token'], TOKEN)


if __name__ == '__main__':
    unittest.main()
