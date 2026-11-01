import unittest
from pathlib import Path

from codex_monitor.client_counter import TITLE_ITEMS, native_thread_prefix, resolve_counter


THREAD = '550e8400-e29b-41d4-a716-446655440000'
COLLISION = '550e8400-e29b-41d4-a716-44665fffffff'
OTHER = '12345678-1234-1234-1234-123456789abc'
HOME = '/profiles/counter-test'
NATIVE_PREFIX = '550e8400-e29b-41d4-a716-44665'
TITLE = 'codex | ' + NATIVE_PREFIX + '... | codex | Fix login timeout | project'


def watcher(thread=THREAD, home=HOME, active=True):
    return {'thread_id': thread, 'codex_home': home, 'active': active}


class ClientCounter(unittest.TestCase):
    def test_native_snapshot_identity_format_in_configured_first_fields(self):
        # Codex 0.166's committed terminal-title snapshot renders this exact
        # 29-character prefix plus three ASCII dots for its thread-id item.
        self.assertEqual(native_thread_prefix(TITLE), NATIVE_PREFIX)
        self.assertEqual(native_thread_prefix('codex | ' + NATIVE_PREFIX + '... | codex'), NATIVE_PREFIX)
        self.assertEqual(TITLE_ITEMS, ['app-name', 'thread-id', 'app-name', 'thread-name', 'project-name'])

    def test_full_id_is_accepted_only_in_the_explicit_field(self):
        self.assertEqual(native_thread_prefix('codex | ' + THREAD + ' | codex'), THREAD)
        self.assertEqual(native_thread_prefix('codex | ' + THREAD.upper() + ' | codex | project'), THREAD)
        for title in ('Fix ' + THREAD, 'project | codex | ' + THREAD,
                      'codex | Fix ' + THREAD, 'codex | name | ' + THREAD,
                      THREAD + ' | codex', 'CODEX | ' + THREAD):
            with self.subTest(title=title):
                self.assertIsNone(native_thread_prefix(title))

    def test_missing_malformed_and_incomplete_identity_fail_closed(self):
        invalid = (None, '', 'codex', 'codex | ', 'codex | ' + NATIVE_PREFIX,
                   'codex | ' + NATIVE_PREFIX[:-1] + '...',
                   'codex | ' + NATIVE_PREFIX + '..', 'codex | ' + THREAD + '...',
                   'codex | ' + NATIVE_PREFIX + '...suffix',
                   'codex | ' + THREAD,
                   'codex | ' + THREAD + ' | codexevil',
                   'codex | ' + THREAD + ' | Codex',
                   'codex | ' + THREAD + ' | ', ' codex | ' + THREAD,
                   'codex | ' + THREAD + '\n')
        for title in invalid:
            with self.subTest(title=title):
                result = resolve_counter(title, [watcher()])
                self.assertEqual(result['state'], 'awaiting_client_identity')
                self.assertIsNone(result['active'])
                self.assertEqual(result['label'], '')

    def test_duplicate_scope_counts_all_watchers_and_normalizes_home(self):
        rows = [watcher(), watcher(home=HOME + '/../counter-test/.'),
                watcher(OTHER), watcher(active=False)]
        result = resolve_counter(TITLE, rows)
        self.assertEqual(result, {'state': 'bound', 'thread_id': THREAD,
                                 'codex_home': str(Path(HOME).resolve()), 'active': 2,
                                 'label': '2 monitors', 'native_prefix': NATIVE_PREFIX})

    def test_same_prefix_with_two_full_thread_ids_is_ambiguous(self):
        result = resolve_counter(TITLE, [watcher(), watcher(COLLISION)])
        self.assertEqual(result['state'], 'ambiguous_client_identity')
        self.assertEqual(result['label'], '')
        self.assertIsNone(result['thread_id'])
        self.assertIsNone(result['active'])

    def test_authoritative_home_excludes_other_profiles_even_if_they_are_the_only_active_match(self):
        other = '/another/profile'
        self.assertEqual(resolve_counter(TITLE, [watcher(home=other)], HOME)['label'], '')
        result = resolve_counter(TITLE, [watcher(), watcher(home=other)], HOME)
        self.assertEqual((result['codex_home'], result['active'], result['label']),
                         (str(Path(HOME).resolve()), 1, '1 monitor'))

    def test_same_full_id_in_two_profiles_is_ambiguous_even_for_full_title_id(self):
        for title in (TITLE, 'codex | ' + THREAD + ' | codex'):
            with self.subTest(title=title):
                result = resolve_counter(title, [watcher(), watcher(home='/another/profile')])
                self.assertEqual(result['state'], 'ambiguous_client_identity')
                self.assertEqual(result['label'], '')
                self.assertIsNone(result['codex_home'])

    def test_stopped_or_invalid_rows_never_create_matches_or_ambiguity(self):
        rows = [watcher(), watcher(COLLISION, active=False), watcher(home='/other', active=0),
                watcher(active='true'), watcher(active=1.0), watcher(home=''),
                watcher(home='relative-profile'), watcher(thread='not-a-thread'), None]
        result = resolve_counter(TITLE, rows)
        self.assertEqual(result['state'], 'bound')
        self.assertEqual(result['active'], 1)

    def test_no_active_matching_scope_is_blank_zero(self):
        for rows in ([], None, [watcher(active=False)], [watcher(OTHER)]):
            with self.subTest(rows=rows):
                result = resolve_counter(TITLE, rows)
                self.assertEqual(result['state'], 'no_active_watchers')
                self.assertEqual(result['active'], 0)
                self.assertEqual(result['label'], '')

    def test_store_shaped_registry_entries_are_supported(self):
        result = resolve_counter(TITLE, [{'active': 1, 'spec': {
            'thread_id': THREAD, 'codex_home': HOME}}])
        self.assertEqual(result['state'], 'bound')
        self.assertEqual(result['active'], 1)

    def test_independent_client_titles_resolve_existing_watchers_without_registration(self):
        rows = [watcher(), watcher(), watcher(OTHER, '/profiles/other')]
        first = resolve_counter(TITLE, rows)
        second = resolve_counter('codex | ' + OTHER[:29] + '... | codex | existing task', rows)
        resumed = resolve_counter(TITLE, rows)
        self.assertEqual((first['thread_id'], first['label']), (THREAD, '2 monitors'))
        self.assertEqual((second['thread_id'], second['label']), (OTHER, '1 monitor'))
        self.assertEqual(resumed, first)

    def test_omitted_native_identity_cannot_shift_uuid_shaped_names_into_its_slot(self):
        for name in (THREAD, NATIVE_PREFIX + '...', 'ordinary task'):
            with self.subTest(name=name):
                title = 'codex | codex | ' + name + ' | project'
                result = resolve_counter(title, [watcher()])
                self.assertEqual(result['state'], 'awaiting_client_identity')
                self.assertEqual(result['label'], '')

    def test_unicode_name_cannot_truncate_the_leading_identity_contract(self):
        title = 'codex | ' + NATIVE_PREFIX + '... | codex | ' + ('e\u0301' * 200)
        # Native overall title sanitization cuts after 240 codepoints. The
        # identity and its closing fixed marker are complete before that cut.
        result = resolve_counter(title[:240], [watcher()])
        self.assertEqual(result['state'], 'bound')
        self.assertEqual(result['label'], '1 monitor')
