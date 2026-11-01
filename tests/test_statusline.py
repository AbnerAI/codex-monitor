import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from codex_monitor.statusline import active_count, build_snapshot, read_label, write_snapshot


class StatusLine(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cm-statusline-test-')
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / 'profile'
        self.home.mkdir()
        self.other_home = self.root / 'other-profile'
        self.watchers = [
            self.watcher('current', self.home, True),
            self.watcher('current', self.home, 1),
            self.watcher('other-thread', self.home, True),
            self.watcher('current', self.other_home, True),
            self.watcher('current', self.home, False),
        ]

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def watcher(thread, home, active):
        return {'active': active, 'spec': {'thread_id': thread, 'codex_home': str(home)}}

    def test_active_count_excludes_other_threads_profiles_and_stopped_watchers(self):
        self.assertEqual(active_count(self.watchers, 'current', self.home), 2)
        self.assertEqual(active_count(self.watchers, 'other-thread', self.home), 1)
        self.assertEqual(active_count(self.watchers, 'current', self.other_home), 1)
        self.assertEqual(active_count(self.watchers, 'missing', self.home), 0)

    def test_normalized_paths_merge_same_profile_and_resolve_symlinks(self):
        alias = self.root / 'profile-alias'
        alias.symlink_to(self.home, target_is_directory=True)
        watchers = [self.watcher('current', alias, True),
                    self.watcher('current', self.home / '..' / 'profile', True)]
        self.assertEqual(active_count(watchers, 'current', self.home), 2)
        snapshot = build_snapshot(watchers, now=1000)
        self.assertEqual(snapshot['counts'], [
            {'thread_id': 'current', 'codex_home': str(self.home), 'active': 2}])
        self.assertEqual(read_label(snapshot, 'current', alias, now=1001), 'monitor-2')

    def test_home_expansion_matches_the_same_profile(self):
        home = Path.home().resolve()
        watchers = [self.watcher('current', '~', True)]
        self.assertEqual(active_count(watchers, 'current', home), 1)

    def test_invalid_watchers_never_increase_the_count(self):
        invalid = [None, {}, {'active': True},
                   self.watcher('current', self.home, 'true'),
                   self.watcher('current', self.home, 2),
                   self.watcher('', self.home, True),
                   self.watcher('current', '', True),
                   self.watcher('current', '\0', True)]
        self.assertEqual(active_count(self.watchers + invalid, 'current', self.home), 2)
        self.assertEqual(build_snapshot(invalid, now=1000)['counts'], [])
        self.assertEqual(active_count(self.watchers, '', self.home), 0)

    def test_snapshot_aggregates_all_profiles_with_stable_scope_and_float_time(self):
        snapshot = build_snapshot(reversed(self.watchers), now=1000)
        self.assertEqual(snapshot['schema'], 1)
        self.assertIs(snapshot['running'], True)
        self.assertIsInstance(snapshot['updated_at'], float)
        self.assertEqual(snapshot['updated_at'], 1000.0)
        self.assertEqual(snapshot, build_snapshot(self.watchers, now=1000))
        counts = {(row['thread_id'], row['codex_home']): row['active']
                  for row in snapshot['counts']}
        self.assertEqual(counts, {('current', str(self.home)): 2,
                                  ('current', str(self.other_home)): 1,
                                  ('other-thread', str(self.home)): 1})

    def test_labels_require_exact_scope_and_show_zero_only_for_healthy_state(self):
        snapshot = build_snapshot(self.watchers, now=1000)
        self.assertEqual(read_label(snapshot, 'current', self.home, now=1002), 'monitor-2')
        self.assertEqual(read_label(snapshot, 'missing', self.home, now=1002), '')
        self.assertEqual(read_label(snapshot, 'missing', self.home,
                                    now=1002, show_zero=True), 'monitor-0')
        self.assertEqual(read_label(snapshot, 'current', self.root / 'absent', now=1002), '')
        self.assertEqual(read_label(snapshot, 'current', self.home, now=1005), 'monitor-2')

    def test_stale_future_and_shutdown_snapshots_hide_counts_even_with_show_zero(self):
        live = build_snapshot(self.watchers, now=1000)
        shutdown = build_snapshot(self.watchers, running=False, now=1000)
        for snapshot, now in [(live, 1005.01), (live, 999.99), (shutdown, 1001)]:
            with self.subTest(snapshot=snapshot, now=now):
                self.assertEqual(read_label(snapshot, 'current', self.home,
                                            now=now, show_zero=True), '')

    def test_invalid_snapshots_fail_closed(self):
        valid = build_snapshot(self.watchers, now=1000)
        malformed = [None, [], {}, dict(valid, schema=2), dict(valid, schema=True),
                     dict(valid, running=1), dict(valid, counts={}),
                     dict(valid, counts=[None]), dict(valid, counts=valid['counts'] * 2)]
        for updated in (None, '1000', True, -1, float('nan'), float('inf'), 10 ** 1000):
            malformed.append(dict(valid, updated_at=updated))
        for count in (-1, True, 2.5, '2'):
            malformed.append(dict(valid, counts=[{
                'thread_id': 'current', 'codex_home': str(self.home), 'active': count}]))
        for snapshot in malformed:
            with self.subTest(snapshot=snapshot):
                self.assertEqual(read_label(snapshot, 'current', self.home,
                                            now=1001, show_zero=True), '')
        self.assertEqual(read_label(valid, 'current', self.home, now=float('nan')), '')
        self.assertEqual(read_label(valid, 'current', self.home, now=1001, max_age=-1), '')

    def test_atomic_private_json_roundtrip_replaces_existing_file_and_shutdown(self):
        directory = self.root / 'state'
        directory.mkdir()
        target = directory / 'statusline.json'
        target.write_text('{"old": true}\n')
        target.chmod(0o644)
        result = write_snapshot(directory, self.watchers, now=1000.25)
        self.assertEqual(result, target)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        snapshot = json.loads(target.read_text())
        self.assertEqual(snapshot, build_snapshot(self.watchers, now=1000.25))
        self.assertEqual(read_label(snapshot, 'current', self.home, now=1001), 'monitor-2')
        self.assertEqual(list(directory.glob('.statusline-*.tmp')), [])
        write_snapshot(directory, self.watchers, running=False, now=1001)
        self.assertEqual(read_label(json.loads(target.read_text()), 'current', self.home,
                                    now=1001, show_zero=True), '')

    def test_failed_replace_preserves_previous_complete_snapshot_and_cleans_tempfile(self):
        target = write_snapshot(self.root / 'state', self.watchers, now=1000)
        previous = target.read_bytes()
        with patch('codex_monitor.statusline.os.replace', side_effect=OSError('replace failed')):
            with self.assertRaisesRegex(OSError, 'replace failed'):
                write_snapshot(target.parent, [], now=1001)
        self.assertEqual(target.read_bytes(), previous)
        self.assertEqual(list(target.parent.glob('.statusline-*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
