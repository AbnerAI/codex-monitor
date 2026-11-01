import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_monitor import cli
from codex_monitor.launch_state import create_lease, launch_lock, lease_path, valid_lease


class WatchDisplayOwnership(unittest.TestCase):
    def test_watch_registration_never_rebinds_display_from_an_inherited_valid_launch_token(self):
        original_terminal = '11111111-2222-4333-8444-555555555555'
        current_thread = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
        inherited_token = '0123456789abcdef0123456789abcdef'
        with tempfile.TemporaryDirectory(prefix='cm-watch-display-') as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            profile = root / 'actual-thread-profile'
            with launch_lock(root, original_terminal):
                create_lease(root, original_terminal, inherited_token)
            self.assertTrue(valid_lease(root, original_terminal, inherited_token))
            before = lease_path(root, original_terminal).read_bytes()
            inherited = {'CODEX_MONITOR_ITERM_SESSION': original_terminal,
                         'CODEX_MONITOR_LAUNCH_TOKEN': inherited_token,
                         'ITERM_SESSION_ID': 'w0t0p0:' + original_terminal,
                         'CODEX_HOME': str(root / 'other-client-profile')}
            registered = {'id': 'monitor-test-owned', 'armed': True,
                          'source': 'file', 'thread_id': current_thread}
            with patch.dict(os.environ, inherited), \
                 patch('codex_monitor.cli.validate_spec') as validate, \
                 patch('codex_monitor.cli.codex_binary', return_value='/original/codex'), \
                 patch('codex_monitor.cli.control', return_value=registered) as control, \
                 patch('codex_monitor.iterm.attach_for_launch') as automatic_binding, \
                 patch('codex_monitor.iterm.action') as gui, \
                 patch('codex_monitor.iterm.detach') as detach:
                args = cli.parser().parse_args([
                    '--state-dir', str(root), 'watch', '--file', str(root / 'inbox.ndjson'),
                    '--thread', current_thread, '--codex-home', str(profile)])
                result = cli.execute(args)
            self.assertEqual(result, registered)
            self.assertTrue(result['armed'])
            self.assertNotIn('iterm', result)
            validate.assert_called_once()
            control.assert_called_once()
            request = control.call_args.args[1]
            self.assertEqual(request['method'], 'watch')
            self.assertEqual(request['spec']['thread_id'], current_thread)
            self.assertEqual(request['spec']['codex_home'], str(profile))
            automatic_binding.assert_not_called()
            gui.assert_not_called()
            detach.assert_not_called()
            self.assertEqual(lease_path(root, original_terminal).read_bytes(), before)
            self.assertFalse((root / 'iterm').exists())


if __name__ == '__main__':
    unittest.main()
