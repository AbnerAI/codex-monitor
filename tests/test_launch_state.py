import json
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest.mock import patch

from codex_monitor.iterm import save
from codex_monitor.launch_state import (create_lease, invalidate_lease, launch_lock,
                                      lease_path, read_lease, refresh_lease,
                                      valid_lease)


SESSION = '11111111-2222-4333-8444-555555555555'
TOKEN = '0123456789abcdef0123456789abcdef'
NEW_TOKEN = 'abcdef0123456789abcdef0123456789'


class LaunchLeases(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cm-launch-lease-')
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)

    def tearDown(self):
        self.temp.cleanup()

    def record(self, **changes):
        value = {'active': True, 'token': TOKEN, 'session_id': SESSION,
                 'pid': os.getpid(), 'updated_at': time.time()}
        value.update(changes)
        target = lease_path(self.root, SESSION)
        target.parent.mkdir(mode=0o700, exist_ok=True)
        save(target, value)
        return value

    def test_live_owner_requires_current_token_and_timely_heartbeat(self):
        self.record(updated_at=1000.0)
        self.assertTrue(valid_lease(self.root, SESSION, TOKEN, now=1000.0))
        self.assertTrue(valid_lease(self.root, SESSION, TOKEN, now=1120.0))
        self.assertFalse(valid_lease(self.root, SESSION, TOKEN, now=1120.01))
        self.assertFalse(valid_lease(self.root, SESSION, TOKEN, now=999.99))
        self.assertFalse(valid_lease(self.root, SESSION, NEW_TOKEN, now=1001.0))
        with patch('codex_monitor.launch_state.os.kill', side_effect=ProcessLookupError):
            self.assertFalse(valid_lease(self.root, SESSION, TOKEN, now=1001.0))

    def test_invalid_or_revoked_leases_cannot_authorize_terminal_changes(self):
        for token in (None, '', 'not-a-token', 'g' * 32, 'a' * 31, 42):
            with self.subTest(token=token):
                self.record()
                self.assertFalse(valid_lease(self.root, SESSION, token))
        for changes in ({'active': False}, {'active': 1}, {'pid': True},
                        {'pid': 1}, {'pid': -10}, {'updated_at': 'now'},
                        {'updated_at': float('nan')}, {'updated_at': float('inf')}):
            with self.subTest(changes=changes):
                self.record(**changes)
                self.assertFalse(valid_lease(self.root, SESSION, TOKEN))
        lease_path(self.root, SESSION).write_text('{partial')
        self.assertIsNone(read_lease(self.root, SESSION))
        self.assertFalse(valid_lease(self.root, SESSION, TOKEN))

    def test_create_refuses_live_owner_but_replaces_an_expired_lease_privately(self):
        with launch_lock(self.root, SESSION):
            created = create_lease(self.root, SESSION.lower(), TOKEN)
            with self.assertRaisesRegex(ValueError, 'already owns'):
                create_lease(self.root, SESSION, NEW_TOKEN)
            self.assertEqual(read_lease(self.root, SESSION)['token'], TOKEN)
            self.record(updated_at=time.time() - 121)
            replacement = create_lease(self.root, SESSION, NEW_TOKEN)
        self.assertEqual(created['pid'], os.getpid())
        self.assertEqual(replacement['token'], NEW_TOKEN)
        self.assertTrue(valid_lease(self.root, SESSION, NEW_TOKEN))
        self.assertEqual(stat.S_IMODE(lease_path(self.root, SESSION).stat().st_mode), 0o600)

    def test_old_launch_cannot_refresh_or_revoke_a_new_owner(self):
        original = self.record(token=NEW_TOKEN, updated_at=time.time() - 1)
        refresh_lease(self.root, SESSION, TOKEN)
        self.assertEqual(read_lease(self.root, SESSION), original)
        self.assertFalse(invalidate_lease(self.root, SESSION, TOKEN))
        self.assertEqual(read_lease(self.root, SESSION), original)
        refresh_lease(self.root, SESSION, NEW_TOKEN)
        self.assertGreater(read_lease(self.root, SESSION)['updated_at'], original['updated_at'])
        self.assertTrue(invalidate_lease(self.root, SESSION, NEW_TOKEN))
        self.assertFalse(valid_lease(self.root, SESSION, NEW_TOKEN))
        revoked = read_lease(self.root, SESSION)
        refresh_lease(self.root, SESSION, NEW_TOKEN)
        self.assertEqual(read_lease(self.root, SESSION), revoked)

    def test_shared_or_foreign_state_directory_is_rejected_before_a_lock_is_created(self):
        self.root.chmod(0o755)
        with self.assertRaisesRegex(ValueError, 'private'):
            with launch_lock(self.root, SESSION):
                self.fail('A shared state directory must not authorize a terminal change')
        self.assertFalse((self.root / 'iterm-launches').exists())
        self.root.chmod(0o700)
        with patch('codex_monitor.launch_state.os.getuid', return_value=os.getuid() + 1):
            with self.assertRaisesRegex(ValueError, 'private'):
                with launch_lock(self.root, SESSION):
                    self.fail('A state directory owned by somebody else must be rejected')
        self.assertFalse((self.root / 'iterm-launches').exists())


if __name__ == '__main__':
    unittest.main()
