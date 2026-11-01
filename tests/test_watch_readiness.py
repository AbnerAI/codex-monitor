from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_monitor import cli
from codex_monitor.rpc import RpcError


THREAD = '550e8400-e29b-41d4-a716-446655440000'


class WatchClientReadiness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cm-watch-preflight-')
        self.root = Path(self.temp.name).resolve()
        self.home = str(self.root / 'profile')
        self.socket = str(self.root / 'actual-control.sock')
        self.args = cli.parser().parse_args([
            '--state-dir', str(self.root / 'monitor-state'), 'watch',
            '--file', str(self.root / 'inbox.ndjson'), '--once', '--thread', THREAD,
            '--codex-home', self.home, '--codex-bin', '/original/codex',
            '--socket', self.socket])
        self.binary = patch('codex_monitor.cli.codex_binary', return_value='/original/codex').start()
        self.factory = patch('codex_monitor.cli.CodexRPC').start()
        self.rpc = self.factory.return_value
        self.rpc.socket_path = self.socket
        self.rpc.unavailable_message.return_value = 'in-process runtime cannot receive through this daemon'
        self.control = patch('codex_monitor.cli.control', return_value={
            'id': 'monitor-returned', 'armed': True, 'thread_id': THREAD}).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.temp.cleanup)

    def test_history_only_target_is_rejected_before_any_old_service_request(self):
        self.rpc.readiness.return_value = {
            'exact_thread_verified': True, 'runtime_loaded': False,
            'thread_status': {'type': 'notLoaded'}, 'registration_ready': False,
            'delivery_ready': False, 'delivery_state': 'thread_not_loaded'}
        with self.assertRaises(RpcError) as error:
            cli.execute(self.args)
        self.assertIn('in-process', str(error.exception))
        self.control.assert_not_called()
        self.factory.assert_called_once_with(self.home, '/original/codex', self.socket)
        self.rpc.readiness.assert_called_once_with(THREAD)
        self.rpc.close.assert_called_once()

    def test_human_approval_wait_still_registers_on_the_same_verified_scope(self):
        self.rpc.readiness.return_value = {
            'exact_thread_verified': True, 'runtime_loaded': True,
            'thread_status': {'type': 'active', 'activeFlags': ['waitingOnApproval']},
            'registration_ready': True, 'delivery_ready': False,
            'delivery_state': 'waiting_for_approval_or_input'}
        result = cli.execute(self.args)
        self.assertTrue(result['armed'])
        self.assertEqual(result['id'], 'monitor-returned')
        self.factory.assert_called_once_with(self.home, '/original/codex', self.socket)
        self.rpc.readiness.assert_called_once_with(THREAD)
        self.rpc.close.assert_called_once()
        self.control.assert_called_once()
        root, request = self.control.call_args.args
        self.assertEqual(root, self.root / 'monitor-state')
        self.assertEqual(request['method'], 'watch')
        self.assertEqual(request['spec']['thread_id'], THREAD)
        self.assertEqual(request['spec']['codex_home'], self.home)
        self.assertEqual(request['spec']['socket'], self.socket)
        self.assertEqual(request['spec']['codex_bin'], '/original/codex')
        self.assertTrue(request['spec']['once'])

    def test_preflight_protocol_failure_cannot_fall_back_to_old_service_registration(self):
        self.rpc.readiness.side_effect = RpcError('thread/loaded/list unsupported')
        with self.assertRaisesRegex(RpcError, 'unsupported'):
            cli.execute(self.args)
        self.control.assert_not_called()
        self.rpc.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
