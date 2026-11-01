import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from codex_monitor import cli, iterm
from codex_monitor.iterm import (SessionIdentityError, SessionMismatchError, action,
                                attach, attachment_path, execute, identify_session,
                                resolve_session, run, save)
from codex_monitor.launch_state import LEASE_MAX_AGE, lease_path, invalidate_lease


A = '11111111-2222-4333-8444-555555555555'
B = 'AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE'
TOKEN = '0123456789abcdef0123456789abcdef'
NEW_TOKEN = 'abcdef0123456789abcdef0123456789'
THREAD = 'current-codex-thread'


class TerminalIdentity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cm-identity-')
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.home = str(self.root / 'actual-profile')
        self.environment = {'ITERM_SESSION_ID': 'w0t0p0:' + A,
                            'CODEX_HOME': str(self.root / 'unrelated-parent-profile')}
        self.env_patch = patch.dict(os.environ, self.environment, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.addCleanup(self.temp.cleanup)

    def scoped(self, **changes):
        os.environ.update(CODEX_MONITOR_ITERM_SESSION=B, CODEX_MONITOR_LAUNCH_TOKEN=TOKEN)
        value = {'active': True, 'session_id': B, 'token': TOKEN,
                 'pid': os.getpid(), 'updated_at': time.time()}
        value.update(changes)
        target = lease_path(self.root, B)
        target.parent.mkdir(mode=0o700, exist_ok=True)
        save(target, value)
        return value

    def args(self, operation='attach', session=None):
        return SimpleNamespace(iterm_action=operation, session=session,
                               thread=THREAD, codex_home=self.home)

    def metadata(self, identifier=B):
        return {'session_id': identifier, 'tty': '/dev/fake-terminal', 'panes': 1,
                'title_format': None, 'tab_title': 'existing title', 'monitor': ''}

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def test_scoped_live_identity_overrides_a_stale_inherited_terminal(self):
        self.scoped()
        identity = resolve_session(self.root)
        self.assertEqual((identity.session_id, identity.source, identity.launch_token),
                         (B, 'validated_launch', TOKEN))
        self.assertNotIn(TOKEN, repr(identity))
        with patch('codex_monitor.iterm.action', return_value=self.metadata()) as gui:
            result = execute(self.root, self.args('status'))
        gui.assert_called_once_with('probe', B)
        self.assertEqual(result['identity_source'], 'validated_launch')
        self.assertEqual(os.environ['ITERM_SESSION_ID'], 'w0t0p0:' + A)

    def test_explicit_confirmation_overrides_invalid_markers_but_empty_explicit_never_falls_back(self):
        os.environ.update(CODEX_MONITOR_ITERM_SESSION='invalid', CODEX_MONITOR_LAUNCH_TOKEN='invalid')
        identity = resolve_session(self.root, B.lower())
        self.assertEqual((identity.session_id, identity.source), (B, 'explicit'))
        with patch('codex_monitor.iterm.action', return_value=self.metadata()) as gui:
            result = execute(self.root, self.args('status', session=B))
        gui.assert_called_once_with('probe', B)
        self.assertEqual(result['current_session']['session_id'], B)
        with self.assertRaises(SessionIdentityError) as error:
            resolve_session(self.root, '')
        self.assertEqual(error.exception.details['source'], 'explicit')

    def test_explicit_same_terminal_keeps_its_valid_launch_ownership(self):
        self.scoped()
        identity = resolve_session(self.root, B)
        self.assertEqual(identity.source, 'explicit')
        self.assertEqual(identity.launch_token, TOKEN)
        self.assertIsNone(resolve_session(self.root, A).launch_token)

    def test_partial_empty_and_malformed_scoped_markers_never_use_legacy(self):
        cases = [({'CODEX_MONITOR_ITERM_SESSION': B}, 'partial_markers'),
                 ({'CODEX_MONITOR_LAUNCH_TOKEN': TOKEN}, 'partial_markers'),
                 ({'CODEX_MONITOR_ITERM_SESSION': '', 'CODEX_MONITOR_LAUNCH_TOKEN': TOKEN}, 'invalid_scoped_session'),
                 ({'CODEX_MONITOR_ITERM_SESSION': B, 'CODEX_MONITOR_LAUNCH_TOKEN': ''}, 'invalid_launch_token'),
                 ({'CODEX_MONITOR_ITERM_SESSION': 'invalid', 'CODEX_MONITOR_LAUNCH_TOKEN': TOKEN}, 'invalid_scoped_session'),
                 ({'CODEX_MONITOR_ITERM_SESSION': B, 'CODEX_MONITOR_LAUNCH_TOKEN': 'g' * 32}, 'invalid_launch_token')]
        for markers, reason in cases:
            with self.subTest(markers=list(markers)), patch.dict(os.environ, dict(self.environment, **markers), clear=True), \
                 patch('codex_monitor.iterm.action') as gui:
                with self.assertRaises(SessionIdentityError) as error:
                    resolve_session(self.root)
                self.assertEqual(error.exception.details['reason'], reason)
                self.assertNotIn(TOKEN, str(error.exception))
                gui.assert_not_called()
        self.assertEqual(self.snapshot(), {})

    def test_expired_inactive_replaced_and_dead_owner_leases_fail_closed(self):
        cases = [({'updated_at': time.time() - LEASE_MAX_AGE - 10}, 'lease_expired'),
                 ({'active': False}, 'lease_inactive'),
                 ({'token': NEW_TOKEN}, 'lease_token_mismatch'),
                 ({'session_id': A}, 'lease_invalid_or_owner_unavailable')]
        for changes, reason in cases:
            with self.subTest(changes=changes):
                self.scoped(**changes)
                with self.assertRaises(SessionIdentityError) as error:
                    resolve_session(self.root)
                self.assertEqual(error.exception.details['reason'], reason)
        self.scoped()
        with patch('codex_monitor.launch_state.os.kill', side_effect=ProcessLookupError):
            with self.assertRaises(SessionIdentityError):
                resolve_session(self.root)

    def test_invalid_scoped_identity_blocks_all_manual_operations_before_gui_access(self):
        self.scoped(active=False)
        before = self.snapshot()
        for operation in ('attach', 'status', 'detach', 'api-status'):
            with self.subTest(operation=operation), patch('codex_monitor.iterm.action') as gui:
                with self.assertRaises(SessionIdentityError):
                    execute(self.root, self.args(operation))
                gui.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_identify_reports_all_full_ids_without_using_gui_focus_as_ownership(self):
        self.scoped()
        before = self.snapshot()
        with patch('codex_monitor.iterm.action', return_value=self.metadata()) as gui, \
             patch('codex_monitor.launch_state.launch_lock', side_effect=AssertionError('identify must not lock')):
            result = identify_session(self.root)
        self.assertTrue(result['read_only'])
        self.assertEqual(result['identity']['session_id'], B)
        self.assertEqual(result['provenance']['legacy_environment']['session_id'], A)
        self.assertEqual(result['provenance']['scoped_launch']['session']['session_id'], B)
        self.assertEqual(result['observed_current_session']['session_id'], B)
        self.assertTrue(result['matches_observed_current'])
        self.assertFalse(result['gui_observation_is_ownership_proof'])
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertEqual(self.snapshot(), before)
        gui.assert_called_once_with('identify', None)

    def test_identify_with_missing_private_state_creates_nothing_and_does_not_select_observed_gui(self):
        os.environ.update(CODEX_MONITOR_ITERM_SESSION=B, CODEX_MONITOR_LAUNCH_TOKEN=TOKEN)
        missing = self.root / 'not-created'
        with patch('codex_monitor.iterm.action', return_value=self.metadata()):
            result = identify_session(missing)
        self.assertFalse(result['identity']['valid'])
        self.assertIsNone(result['identity']['session_id'])
        self.assertEqual(result['provenance']['scoped_launch']['validation_reason'], 'state_directory_unavailable')
        self.assertEqual(result['observed_current_session']['session_id'], B)
        self.assertFalse(missing.exists())

    def test_nonprivate_state_cannot_establish_trusted_scoped_identity(self):
        self.scoped()
        self.root.chmod(0o755)
        try:
            with self.assertRaises(SessionIdentityError) as error:
                resolve_session(self.root)
            self.assertEqual(error.exception.details['reason'], 'state_directory_not_private')
        finally:
            self.root.chmod(0o700)

    def test_legacy_mismatch_is_structured_and_never_reaches_attachment_mutations(self):
        with patch('codex_monitor.iterm.action', side_effect=SessionMismatchError(A, B)) as gui, \
             patch('codex_monitor.iterm.CodexRPC') as rpc, \
             patch('codex_monitor.iterm.subprocess.Popen') as popen:
            with self.assertRaises(SessionMismatchError) as error:
                attach(self.root, self.args())
        self.assertEqual(error.exception.details['source'], 'legacy_environment')
        self.assertEqual(error.exception.details['expected_session_id'], A)
        self.assertEqual(error.exception.details['observed_session_id'], B)
        self.assertIn('--session ' + B, error.exception.details['confirmation_command'])
        self.assertIn('--thread ' + THREAD, error.exception.details['confirmation_command'])
        gui.assert_called_once_with('probe', A)
        rpc.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(self.snapshot(), {})

    def test_matching_legacy_cannot_override_a_scoped_mismatch(self):
        self.scoped()
        with patch('codex_monitor.iterm.action', side_effect=SessionMismatchError(B, A)) as gui:
            with self.assertRaises(SessionMismatchError) as error:
                execute(self.root, self.args('status'))
        gui.assert_called_once_with('probe', B)
        self.assertEqual(error.exception.details['source'], 'validated_launch')
        self.assertEqual(error.exception.details['expected_session_id'], B)
        self.assertEqual(error.exception.details['observed_session_id'], A)

    def test_manual_attach_uses_actual_thread_profile_and_retains_launch_token_for_exit_cleanup(self):
        self.scoped()
        rows = [{'active': True, 'thread_id': THREAD, 'codex_home': self.home}] * 2

        def gui(operation, identifier, payload=None):
            self.assertEqual(identifier, B)
            metadata = self.metadata()
            if operation == 'variables':
                metadata['monitor'] = payload['label']
            return metadata

        with patch('codex_monitor.iterm.action', side_effect=gui) as actions, \
             patch('codex_monitor.iterm.CodexRPC') as rpc, \
             patch('codex_monitor.iterm.codex_binary', return_value='/fake/codex'), \
             patch('codex_monitor.iterm.control', return_value=rows), \
             patch('codex_monitor.iterm.subprocess.Popen', return_value=Mock(pid=1234)):
            rpc.return_value.target.return_value = {'id': THREAD}
            result = attach(self.root, self.args(session=B))
        self.assertTrue(result['attached'])
        self.assertEqual(result['identity_source'], 'explicit')
        plan = json.loads(attachment_path(self.root, B).read_text())
        self.assertEqual((plan['session_id'], plan['thread_id'], plan['codex_home'], plan['launch_token']),
                         (B, THREAD, self.home, TOKEN))
        self.assertEqual(plan['label'], '2 monitors')
        self.assertTrue(all(call.args[1] == B for call in actions.call_args_list))
        rpc.return_value.target.assert_called_once_with(THREAD)

    def test_legacy_manual_attach_without_explicit_session_retains_confirmation_in_error(self):
        self.scoped()
        with patch('codex_monitor.iterm.action') as gui:
            with self.assertRaises(SessionIdentityError) as error:
                attach(self.root, self.args())
        self.assertEqual(error.exception.details['code'], 'iterm_explicit_confirmation_required')
        self.assertEqual(error.exception.details['reason'], 'explicit_confirmation_required')
        self.assertIn('--session ' + B, error.exception.details['confirmation_command'])
        gui.assert_not_called()
        self.assertFalse(attachment_path(self.root, B).exists())

    def test_revocation_between_resolution_and_attachment_is_rechecked_before_mutations(self):
        self.scoped()
        resolver = resolve_session

        def revoke_after_resolving(root, explicit=None):
            identity = resolver(root, explicit)
            invalidate_lease(root, B, TOKEN)
            return identity

        with patch('codex_monitor.iterm.resolve_session', side_effect=revoke_after_resolving), \
             patch('codex_monitor.iterm.action') as gui, \
             patch('codex_monitor.iterm.CodexRPC') as rpc:
            with self.assertRaises(SessionIdentityError):
                attach(self.root, self.args())
        gui.assert_not_called()
        rpc.assert_not_called()
        self.assertFalse(attachment_path(self.root, B).exists())

    def test_changed_scoped_marker_between_resolution_and_binding_cannot_retarget_it(self):
        self.scoped()
        resolver = resolve_session

        def replace_after_resolving(root, explicit=None):
            identity = resolver(root, explicit)
            os.environ['CODEX_MONITOR_LAUNCH_TOKEN'] = NEW_TOKEN
            return identity

        with patch('codex_monitor.iterm.resolve_session', side_effect=replace_after_resolving), \
             patch('codex_monitor.iterm.action') as gui:
            with self.assertRaises(SessionIdentityError):
                attach(self.root, self.args())
        gui.assert_not_called()
        self.assertFalse(attachment_path(self.root, B).exists())

    def test_lease_is_rechecked_after_rpc_before_writing_the_terminal(self):
        self.scoped()

        def target(_):
            invalidate_lease(self.root, B, TOKEN)
            return {'id': THREAD}

        with patch('codex_monitor.iterm.action', return_value=self.metadata()) as gui, \
             patch('codex_monitor.iterm.CodexRPC') as rpc, \
             patch('codex_monitor.iterm.codex_binary', return_value='/fake/codex'), \
             patch('codex_monitor.iterm.control', return_value=[]), \
             patch('codex_monitor.iterm.subprocess.Popen') as popen:
            rpc.return_value.target.side_effect = target
            with self.assertRaises(SessionIdentityError) as error:
                attach(self.root, self.args(session=B))
        self.assertEqual(error.exception.details['reason'], 'lease_invalid_before_mutation')
        gui.assert_called_once_with('probe', B)
        popen.assert_not_called()
        self.assertFalse(attachment_path(self.root, B).exists())

    def test_cli_mismatch_keeps_human_error_and_machine_readable_full_identifiers(self):
        with patch('sys.argv', ['codex-monitor', '--state-dir', str(self.root), 'iterm', 'status']), \
             patch('codex_monitor.iterm.action', side_effect=SessionMismatchError(A, B)), \
             patch('sys.stderr', new_callable=io.StringIO) as error:
            with self.assertRaises(SystemExit) as exited:
                cli.main()
        self.assertEqual(exited.exception.code, 1)
        result = json.loads(error.getvalue())
        self.assertEqual(result['code'], 'iterm_session_mismatch')
        self.assertEqual((result['expected_session_id'], result['observed_session_id']), (A, B))
        self.assertEqual(result['source'], 'legacy_environment')
        self.assertIn(A, result['error'])
        self.assertIn(B, result['error'])
        self.assertIn('--session ' + B, result['confirmation_command'])


class MismatchTransport(unittest.TestCase):
    def test_resource_mismatch_payload_becomes_a_typed_python_error(self):
        reply = {'error': {'code': 'iterm_session_mismatch',
                           'expected_session_id': A, 'observed_session_id': B}}
        completed = subprocess.CompletedProcess([], 0, json.dumps(reply), '')
        with patch('codex_monitor.iterm.sys.platform', 'darwin'), \
             patch('codex_monitor.iterm.subprocess.run', return_value=completed):
            with self.assertRaises(SessionMismatchError) as error:
                action('variables', A, {'label': '2 monitors'})
        self.assertEqual(error.exception.details['expected_session_id'], A)
        self.assertEqual(error.exception.details['observed_session_id'], B)

    def test_updater_pauses_on_other_focus_then_resumes_only_its_original_binding(self):
        with tempfile.TemporaryDirectory(prefix='cm-identity-updater-') as temporary:
            root = Path(temporary)
            home = str(root / 'profile')
            path = attachment_path(root, B)
            path.parent.mkdir()
            save(path, {'session_id': B, 'state_dir': str(root), 'thread_id': THREAD,
                        'codex_home': home, 'active': True, 'label': '', 'count': 0})
            probes = []
            writes = []
            snapshots = []

            def gui(operation, identifier, payload=None):
                if operation == 'probe':
                    probes.append(identifier)
                    if len(probes) == 1:
                        raise SessionMismatchError(B, A)
                    return {'session_id': B, 'monitor': ''}
                writes.append((identifier, payload['label']))
                return {'session_id': B, 'monitor': payload['label']}

            def tick(_):
                snapshots.append(json.loads(path.read_text()))
                if len(snapshots) == 2:
                    path.with_suffix('.stop').touch()

            rows = [{'active': True, 'thread_id': THREAD, 'codex_home': home}] * 2
            with patch('codex_monitor.iterm.action', side_effect=gui), \
                 patch('codex_monitor.iterm.control', return_value=rows), \
                 patch('codex_monitor.iterm.time.sleep', side_effect=tick):
                run(path)
            self.assertEqual(probes, [B, B])
            self.assertEqual(writes, [(B, '2 monitors')])
            paused, resumed = snapshots
            self.assertEqual(paused['stage'], 'paused')
            self.assertEqual(paused['session_id'], B)
            self.assertEqual(paused['last_error_details']['expected_session_id'], B)
            self.assertEqual(paused['last_error_details']['observed_session_id'], A)
            self.assertEqual(paused['last_error_details']['source'], 'attachment')
            self.assertEqual(resumed['stage'], 'attached')
            self.assertEqual(resumed['session_id'], B)
            self.assertNotIn('last_error_details', resumed)
            self.assertEqual((resumed['thread_id'], resumed['codex_home']), (THREAD, home))


if __name__ == '__main__':
    unittest.main()
