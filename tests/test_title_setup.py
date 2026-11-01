import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import call, patch

from codex_monitor.client_counter import TITLE_ITEMS
from codex_monitor.rpc import RpcError, UncertainDelivery
from codex_monitor.title_setup import configure_native_title, TitleSetupError


class NativeTitleSetup(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cm-title-setup-')
        self.home = Path(self.temp.name).resolve()
        self.file = str(self.home / 'config.toml')
        self.original = {'model': 'keep-model', 'approval_policy': 'on-request',
                         'tui': {'theme': 'keep-theme', 'terminal_title': ['project-name']}}
        self.before = self.reply(self.original)
        self.after_config = copy.deepcopy(self.original)
        self.after_config['tui']['terminal_title'] = list(TITLE_ITEMS)
        self.after = self.reply(self.after_config, 'version-2')
        self.write = {'status': 'ok', 'filePath': self.file, 'version': 'version-2',
                      'overriddenMetadata': None}
        self.factory = patch('codex_monitor.title_setup.CodexRPC').start()
        self.rpc = self.factory.return_value
        self.addCleanup(patch.stopall)
        self.addCleanup(self.temp.cleanup)

    def reply(self, config, version='version-1'):
        return {'config': copy.deepcopy(config), 'layers': [
            {'name': {'type': 'user', 'file': self.file, 'profile': None},
             'config': copy.deepcopy(config), 'version': version}]}

    def test_default_preview_reads_only_and_reports_one_key_change(self):
        self.rpc.request.return_value = self.before
        result = configure_native_title(self.home, '/original/codex', '/selected/socket')
        self.factory.assert_called_once_with(str(self.home), '/original/codex', '/selected/socket')
        self.rpc.request.assert_called_once_with('config/read', {'includeLayers': True})
        self.assertEqual(result['terminal_title'], TITLE_ITEMS)
        self.assertEqual(result['previous_terminal_title'], ['project-name'])
        self.assertTrue(result['would_change'])
        self.assertFalse(result['applied'])
        self.assertFalse((self.home / 'config.toml').exists())
        self.rpc.close.assert_called_once()

    def test_apply_writes_only_title_using_verified_file_and_cas_then_reads_back(self):
        before = copy.deepcopy(self.before)
        self.rpc.request.side_effect = [self.before, self.write, self.after]
        result = configure_native_title(str(self.home), '/codex', apply=True)
        self.assertEqual(self.rpc.request.call_args_list, [
            call('config/read', {'includeLayers': True}),
            call('config/value/write', {'keyPath': 'tui.terminal_title', 'value': TITLE_ITEMS,
                 'mergeStrategy': 'replace', 'filePath': self.file,
                 'expectedVersion': 'version-1'}, mutating=True),
            call('config/read', {'includeLayers': True})])
        self.assertTrue(result['applied'])
        self.assertTrue(result['affects_next_launch'])
        self.assertTrue(result['effective_matches'])
        self.assertEqual(self.before, before)
        self.rpc.close.assert_called_once()

    def test_already_configured_is_read_only_even_with_apply(self):
        self.rpc.request.return_value = self.after
        result = configure_native_title(self.home, '/codex', apply=True)
        self.assertFalse(result['would_change'])
        self.assertFalse(result['applied'])
        self.rpc.request.assert_called_once_with('config/read', {'includeLayers': True})

    def test_missing_wrong_profile_duplicate_and_disabled_layers_never_write(self):
        cases = []
        for kind in ('missing', 'wrong-home', 'profile', 'duplicate', 'disabled', 'no-version'):
            reply = copy.deepcopy(self.before)
            layer = reply['layers'][0]
            if kind == 'missing': reply['layers'] = []
            elif kind == 'wrong-home': layer['name']['file'] = '/other/home/config.toml'
            elif kind == 'profile': layer['name']['profile'] = 'company'
            elif kind == 'duplicate': reply['layers'].append(copy.deepcopy(layer))
            elif kind == 'disabled': layer['disabledReason'] = 'managed restriction'
            elif kind == 'no-version': layer['version'] = None
            cases.append((kind, reply))
        for kind, reply in cases:
            with self.subTest(kind=kind):
                self.rpc.reset_mock()
                self.rpc.request.side_effect = None
                self.rpc.request.return_value = reply
                with self.assertRaises(TitleSetupError):
                    configure_native_title(self.home, '/codex', apply=True)
                self.rpc.request.assert_called_once_with('config/read', {'includeLayers': True})
                self.rpc.close.assert_called_once()

    def test_conflict_and_uncertain_writes_are_not_retried(self):
        for error in (RpcError('configVersionConflict'), UncertainDelivery('lost acknowledgement')):
            with self.subTest(error=type(error).__name__):
                self.rpc.reset_mock()
                self.rpc.request.side_effect = [self.before, error]
                with self.assertRaises(type(error)):
                    configure_native_title(self.home, '/codex', apply=True)
                self.assertEqual(self.rpc.request.call_count, 2)
                self.rpc.close.assert_called_once()

    def test_overridden_acknowledgement_is_reported_as_written_not_silently_retried(self):
        write = dict(self.write, status='okOverridden', overriddenMetadata={'effectiveValue': []})
        self.rpc.request.side_effect = [self.before, write]
        with self.assertRaises(TitleSetupError) as caught:
            configure_native_title(self.home, '/codex', apply=True)
        self.assertTrue(caught.exception.details['applied'])
        self.assertEqual(self.rpc.request.call_count, 2)

    def test_wrong_write_target_and_failed_readback_do_not_report_success(self):
        for responses in ([self.before, dict(self.write, filePath='/other/config.toml')],
                          [self.before, self.write, self.before]):
            with self.subTest(responses=len(responses)):
                self.rpc.request.side_effect = responses
                with self.assertRaises(TitleSetupError) as caught:
                    configure_native_title(self.home, '/codex', apply=True)
                self.assertTrue(caught.exception.details['applied'])

    def test_existing_title_overridden_by_another_layer_does_not_write(self):
        reply = copy.deepcopy(self.after)
        reply['config']['tui']['terminal_title'] = ['project-name']
        self.rpc.request.return_value = reply
        with self.assertRaisesRegex(TitleSetupError, 'another layer overrides'):
            configure_native_title(self.home, '/codex', apply=True)
        self.rpc.request.assert_called_once_with('config/read', {'includeLayers': True})

    def test_readback_connection_failure_reports_already_written_and_closes(self):
        self.rpc.request.side_effect = [self.before, self.write, RpcError('connection closed')]
        with self.assertRaisesRegex(TitleSetupError, 'written but readback') as caught:
            configure_native_title(self.home, '/codex', apply=True)
        self.assertTrue(caught.exception.details['applied'])
        self.assertEqual(self.rpc.request.call_count, 3)
        self.rpc.close.assert_called_once()

    def test_relative_claude_and_redirected_config_paths_never_connect(self):
        for home in ('relative/home', self.home / '.claude'):
            with self.subTest(home=home), self.assertRaises(ValueError):
                configure_native_title(home, '/codex', apply=True)
        (self.home / 'config.toml').symlink_to(self.home / 'other.toml')
        with self.assertRaises(ValueError):
            configure_native_title(self.home, '/codex', apply=True)
        self.factory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
