import copy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codex_monitor.cli import doctor
from codex_monitor.daemon import Engine
from codex_monitor.rpc import CodexRPC, Deferred, RpcError


THREAD = '550e8400-e29b-41d4-a716-446655440000'
OTHER = '12345678-1234-1234-1234-123456789abc'


def target(status='idle', **changes):
    value = {'id': THREAD, 'status': status if isinstance(status, dict) else {'type': status},
             'canAcceptDirectInput': True}
    value.update(changes)
    return value


class RuntimeReadiness(unittest.TestCase):
    def setUp(self):
        self.rpc = CodexRPC('/exact/profile', '/unused/codex', '/exact/control.sock')

    def inspect(self, thread, loaded=None):
        pages = {'data': [THREAD] if loaded is None else loaded, 'nextCursor': None}
        with patch.object(self.rpc, 'target', return_value=thread), \
             patch.object(self.rpc, 'request', return_value=pages):
            return self.rpc.readiness(THREAD)

    def test_persisted_in_process_thread_is_not_runtime_ownership(self):
        with patch.object(self.rpc, 'target', return_value=target('notLoaded')), \
             patch.object(self.rpc, 'request', return_value={'data': [], 'nextCursor': None}) as request:
            result = self.rpc.readiness(THREAD)
            self.assertTrue(result['exact_thread_verified'])
            self.assertFalse(result['runtime_loaded'])
            self.assertFalse(result['delivery_ready'])
            self.assertFalse(result['registration_ready'])
            self.assertEqual(result['delivery_state'], 'thread_not_loaded')
            with self.assertRaises(Deferred) as error:
                self.rpc.deliver(THREAD, {'events': []})
        self.assertIn('in-process', str(error.exception))
        self.assertTrue(all(call.args[0] == 'thread/loaded/list' for call in request.call_args_list))

    def test_idle_and_running_threads_in_this_runtime_are_ready(self):
        for status in ('idle', 'active'):
            with self.subTest(status=status):
                result = self.inspect(target(status))
                self.assertTrue(result['runtime_loaded'])
                self.assertTrue(result['delivery_ready'])
                self.assertTrue(result['registration_ready'])
                self.assertEqual(result['delivery_state'], 'ready')

    def test_waiting_for_human_input_can_register_but_cannot_deliver_yet(self):
        for flag in ('waitingOnApproval', 'waitingOnUserInput'):
            with self.subTest(flag=flag):
                result = self.inspect(target(status={'type': 'active', 'activeFlags': [flag]}))
                self.assertTrue(result['runtime_loaded'])
                self.assertTrue(result['registration_ready'])
                self.assertFalse(result['delivery_ready'])
                self.assertEqual(result['delivery_state'], 'waiting_for_approval_or_input')

    def test_loaded_runtime_does_not_override_incompatible_thread_state(self):
        cases = [(target('notLoaded'), 'thread_state_inconsistent'),
                 (target('systemError'), 'system_error'),
                 (target('future-status'), 'unsupported_thread_status'),
                 (target(status={}), 'unsupported_thread_status'),
                 (target('idle', canAcceptDirectInput=False), 'direct_input_not_supported')]
        for thread, state in cases:
            with self.subTest(state=state, thread=thread):
                result = self.inspect(thread)
                self.assertTrue(result['runtime_loaded'])
                self.assertFalse(result['delivery_ready'])
                self.assertFalse(result['registration_ready'])
                self.assertEqual(result['delivery_state'], state)

    def test_wrong_history_id_cannot_be_promoted_by_a_loaded_list_match(self):
        with patch.object(self.rpc, 'target', return_value=target(id=OTHER)), \
             patch.object(self.rpc, 'request') as request:
            result = self.rpc.readiness(THREAD)
        self.assertFalse(result['exact_thread_verified'])
        self.assertFalse(result['registration_ready'])
        self.assertFalse(result['delivery_ready'])
        self.assertEqual(result['delivery_state'], 'thread_id_mismatch')
        request.assert_not_called()

    def test_loaded_list_pagination_uses_the_reported_cursor(self):
        pages = [{'data': [OTHER], 'nextCursor': 'page-two'},
                 {'data': [THREAD], 'nextCursor': None}]
        with patch.object(self.rpc, 'target', return_value=target()), \
             patch.object(self.rpc, 'request', side_effect=pages) as request:
            result = self.rpc.readiness(THREAD)
        self.assertTrue(result['delivery_ready'])
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[1].args[1]['cursor'], 'page-two')
        self.assertTrue(all(call.args[0] == 'thread/loaded/list' for call in request.call_args_list))

    def test_unsupported_or_malformed_loaded_list_cannot_claim_readiness(self):
        replies = [RpcError('method not found'), {}, {'data': THREAD}, {'data': [None]},
                   {'data': [], 'nextCursor': ''}]
        for reply in replies:
            with self.subTest(reply=reply), \
                 patch.object(self.rpc, 'target', return_value=target()), \
                 patch.object(self.rpc, 'request', **({'side_effect': reply}
                     if isinstance(reply, Exception) else {'return_value': reply})):
                result = self.rpc.readiness(THREAD)
            self.assertFalse(result['delivery_ready'])
            self.assertFalse(result['registration_ready'])
            self.assertIsNone(result['runtime_loaded'])
            self.assertEqual(result['delivery_state'], 'runtime_verification_failed')

    def test_repeated_cursor_is_bounded_and_fails_closed(self):
        with patch.object(self.rpc, 'target', return_value=target()), \
             patch.object(self.rpc, 'request', return_value={'data': [], 'nextCursor': 'same'}) as request:
            result = self.rpc.readiness(THREAD)
        self.assertEqual(request.call_count, 2)
        self.assertFalse(result['registration_ready'])
        self.assertEqual(result['delivery_state'], 'runtime_verification_failed')


class DoctorReadiness(unittest.TestCase):
    def test_cli_doctor_does_not_exit_success_for_a_history_only_match(self):
        from codex_monitor.cli import main
        report = {'exact_thread_verified': True, 'runtime_loaded': False,
                  'registration_ready': False, 'delivery_ready': False,
                  'delivery_state': 'thread_not_loaded'}
        args = SimpleNamespace(action='doctor')
        with patch('codex_monitor.cli.parser') as parser, \
             patch('codex_monitor.cli.execute', return_value=report), \
             patch('sys.stdout', new_callable=io.StringIO) as output:
            parser.return_value.parse_args.return_value = args
            with self.assertRaises(SystemExit) as exit_status:
                main()
        self.assertEqual(exit_status.exception.code, 2)
        self.assertEqual(json.loads(output.getvalue()), report)

    def test_doctor_preserves_history_match_but_reports_runtime_mismatch(self):
        args = SimpleNamespace(codex_bin='/unused/codex', codex_home='/exact/profile',
                               socket='/exact/control.sock', thread=THREAD)
        rpc = CodexRPC('/exact/profile', '/unused/codex', args.socket)
        with patch('codex_monitor.cli.codex_binary', return_value='/unused/codex'), \
             patch('codex_monitor.cli.CodexRPC', return_value=rpc), \
             patch.object(rpc, 'connect') as connect, patch.object(rpc, 'close') as close, \
             patch.object(rpc, 'target', return_value=target('notLoaded')), \
             patch.object(rpc, 'request', return_value={'data': [], 'nextCursor': None}):
            report = doctor(args)
        self.assertTrue(report['app_server_connected'])
        self.assertTrue(report['exact_thread_verified'])
        self.assertFalse(report['runtime_loaded'])
        self.assertFalse(report['registration_ready'])
        self.assertFalse(report['delivery_ready'])
        self.assertEqual(report['delivery_state'], 'thread_not_loaded')
        self.assertIn('in-process', report['note'])
        connect.assert_called_once()
        close.assert_called_once()

    def test_doctor_without_thread_does_not_report_delivery_readiness(self):
        args = SimpleNamespace(codex_bin='/unused/codex', codex_home='/exact/profile',
                               socket='/exact/control.sock', thread=None)
        with patch('codex_monitor.cli.codex_binary', return_value='/unused/codex'), \
             patch('codex_monitor.cli.CodexRPC') as factory:
            result = doctor(args)
        self.assertFalse(result['exact_thread_verified'])
        self.assertFalse(result['delivery_ready'])
        self.assertFalse(result['registration_ready'])
        self.assertEqual(result['delivery_state'], 'missing_thread_id')
        factory.return_value.readiness.assert_not_called()


class RegistrationReadiness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cm-readiness-')
        self.root = Path(self.temp.name)
        self.spec = {'source': 'file', 'path': str(self.root / 'inbox'),
                     'thread_id': THREAD, 'codex_home': '/exact/profile',
                     'codex_bin': '/unused/codex', 'socket': '/exact/control.sock',
                     'interval': 1, 'once': True}
        self.rpc = CodexRPC('/exact/profile', '/unused/codex', self.spec['socket'])
        self.engine = Engine(self.root / 'state', lambda *args: self.rpc)
        self.addCleanup(self.temp.cleanup)

    def test_new_registration_rejects_history_only_before_source_and_store_mutations(self):
        with patch.object(self.rpc, 'target', return_value=target('notLoaded')), \
             patch.object(self.rpc, 'request', return_value={'data': [], 'nextCursor': None}), \
             patch.object(self.rpc, 'close') as close, \
             patch('codex_monitor.daemon.initial_cursor') as cursor, \
             patch.object(self.engine.store, 'add') as add, \
             patch.object(self.engine, 'start_worker') as start:
            with self.assertRaises(RpcError) as error:
                self.engine.arm(copy.deepcopy(self.spec))
        self.assertIn('in-process', str(error.exception))
        cursor.assert_not_called()
        add.assert_not_called()
        start.assert_not_called()
        close.assert_called_once()
        self.assertEqual(self.engine.store.watchers(), [])

    def test_human_wait_does_not_reject_a_valid_registration(self):
        waiting = target(status={'type': 'active', 'activeFlags': ['waitingOnApproval']})
        with patch.object(self.rpc, 'target', return_value=waiting), \
             patch.object(self.rpc, 'request', return_value={'data': [THREAD], 'nextCursor': None}), \
             patch.object(self.engine, 'start_worker') as start:
            result = self.engine.arm(copy.deepcopy(self.spec))
        self.assertTrue(result['armed'])
        self.assertEqual(len(self.engine.store.watchers()), 1)
        start.assert_called_once_with(result['id'])

    def test_existing_once_registration_retains_pending_events_when_runtime_unloads(self):
        watcher_id = self.engine.store.add(self.spec, {'offset': 0})
        self.engine.store.checkpoint(watcher_id, {'offset': 1}, [{'content': 'completed'}])
        events = self.engine.store.events(watcher_id, 'pending')
        with patch.object(self.rpc, 'target', return_value=target('notLoaded')), \
             patch.object(self.rpc, 'request', return_value={'data': [], 'nextCursor': None}) as request:
            with self.assertRaises(Deferred):
                self.engine.deliver(self.engine.store.get(watcher_id), events, self.rpc)
        self.assertTrue(self.engine.store.get(watcher_id)['active'])
        saved = self.engine.store.events(watcher_id)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]['status'], 'pending')
        self.assertIn('in-process', saved[0]['error'])
        self.assertFalse(any(call.args[0] == 'turn/start' for call in request.call_args_list))


if __name__ == '__main__':
    unittest.main()
