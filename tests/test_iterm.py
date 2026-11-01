import unittest
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from codex_monitor.iterm import session_id, label_for_rows, attach, attachment_path, detach, run


class ItermScope(unittest.TestCase):
    def test_session_identifier_is_exact_and_validated(self):
        value = '11111111-2222-4333-8444-555555555555'
        self.assertEqual(session_id('w0t8p0:' + value), value)
        with self.assertRaises(ValueError):
            session_id('not-a-session')
        with patch.dict(os.environ, {'ITERM_SESSION_ID': value}):
            with self.assertRaises(ValueError):
                session_id('')
            self.assertEqual(session_id(None), value)

    def test_failed_attach_restores_only_its_own_variable(self):
        identifier = '11111111-2222-4333-8444-555555555555'
        args = SimpleNamespace(session=identifier, thread='current', codex_home='/profile')
        metadata = {'tty': '/dev/fake', 'title_format': 'original', 'monitor': 'previous'}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('codex_monitor.iterm.action', side_effect=[metadata, RuntimeError('failed'), metadata]) as action, \
                 patch('codex_monitor.iterm.CodexRPC') as rpc, \
                 patch('codex_monitor.iterm.control', return_value=[]):
                rpc.return_value.target.return_value = {'id': 'current'}
                with self.assertRaisesRegex(RuntimeError, 'Attach failed'):
                    attach(root, args)
                self.assertEqual(action.call_args_list[-1].args,
                                 ('variables', identifier, {'label': 'previous'}))
                self.assertFalse(attachment_path(root, identifier).exists())

    def test_detach_does_not_overwrite_a_changed_title(self):
        identifier = '11111111-2222-4333-8444-555555555555'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = attachment_path(root, identifier)
            path.parent.mkdir()
            path.write_text(json.dumps({'active': False, 'original_monitor': 'before',
                                        'original_format': 'old title'}))
            with patch('codex_monitor.iterm.action', return_value={'title_format': 'new title', 'monitor': 'before'}) as action:
                result = detach(root, identifier)
                self.assertTrue(result['detached'])
                self.assertEqual(action.call_args_list[-1].args,
                                 ('variables', identifier, {'label': 'before'}))
                self.assertFalse(path.exists())

    def test_other_conversations_profiles_and_stopped_watchers_are_excluded(self):
        rows = [{'active': True, 'thread_id': 'current', 'codex_home': '/exact/home'}] * 2
        rows += [{'active': True, 'thread_id': 'other', 'codex_home': '/exact/home'},
                 {'active': True, 'thread_id': 'current', 'codex_home': '/other/home'},
                 {'active': False, 'thread_id': 'current', 'codex_home': '/exact/home'}]
        self.assertEqual(label_for_rows(rows, 'current', '/exact/home'), (2, '2 monitors'))
        self.assertEqual(label_for_rows(rows, 'missing', '/exact/home'), (0, ''))

    def test_stop_during_probe_prevents_a_late_counter_write(self):
        identifier = '11111111-2222-4333-8444-555555555555'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = attachment_path(root, identifier)
            path.parent.mkdir()
            path.write_text(json.dumps({'active': True, 'session_id': identifier,
                'state_dir': str(root), 'thread_id': 'current', 'codex_home': '/exact/home'}))
            def stop_after_probe(*args):
                path.with_suffix('.stop').touch()
                return {'monitor': ''}
            rows = [{'active': True, 'thread_id': 'current', 'codex_home': '/exact/home'}]
            with patch('codex_monitor.iterm.action', side_effect=stop_after_probe) as action, \
                 patch('codex_monitor.iterm.control', return_value=rows):
                run(path)
            self.assertEqual([call.args[0] for call in action.call_args_list], ['probe'])
            self.assertFalse(json.loads(path.read_text())['active'])
