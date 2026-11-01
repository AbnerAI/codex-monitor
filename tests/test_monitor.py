import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from codex_monitor.daemon import Engine, validate_spec
from codex_monitor.rpc import Deferred, UncertainDelivery
from codex_monitor.sources import bounded, initial_cursor, poll_command, read_file, run_command
from codex_monitor.store import Store

THREAD = '018f1111-2222-7333-8444-555555555555'


class Sources(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cm-test-')
        self.root = Path(self.temp.name)
        self.path = self.root / 'inbox'
        self.spec = {'source': 'file', 'path': str(self.path)}

    def tearDown(self):
        self.temp.cleanup()

    def test_existing_backlog_skipped(self):
        self.path.write_text('old\n')
        cursor = initial_cursor(self.spec)
        with self.path.open('ab') as stream:
            stream.write(b'new\n')
        _, messages = read_file(self.spec, cursor)
        self.assertEqual([m['content'] for m in messages], ['new'])

    def test_partial_json_and_unicode_are_not_lost_or_delivered_early(self):
        cursor = initial_cursor(self.spec)
        data = json.dumps({'content': '你好🙂'}, ensure_ascii=False).encode()
        split = data.index('你'.encode()) + 1
        self.path.write_bytes(data[:split])
        cursor, messages = read_file(self.spec, cursor)
        self.assertEqual(messages, [])
        self.assertEqual(cursor['offset'], 0)
        with self.path.open('ab') as stream:
            stream.write(data[split:] + b'\n')
        cursor, messages = read_file(self.spec, cursor)
        self.assertEqual(messages[0]['content'], '你好🙂')
        self.assertEqual(read_file(self.spec, cursor)[1], [])

    def test_registration_during_an_incomplete_existing_record(self):
        self.path.write_bytes(b'old\n{"content":"par')
        cursor = initial_cursor(self.spec)
        with self.path.open('ab') as stream:
            stream.write(b'tial"}\n')
        _, messages = read_file(self.spec, cursor)
        self.assertEqual([m['content'] for m in messages], ['partial'])

    def test_rotation_and_truncation(self):
        self.path.write_bytes(b'a long old line\n')
        cursor = initial_cursor(self.spec)
        self.path.write_bytes(b'new\n')
        cursor, messages = read_file(self.spec, cursor)
        self.assertEqual(messages[0]['content'], 'new')
        self.path.rename(self.root / 'previous')
        self.path.write_bytes(b'rotated\n')
        self.assertEqual(read_file(self.spec, cursor)[1][0]['content'], 'rotated')

    def test_malformed_json_is_data(self):
        self.path.write_text('{bad json}\n\n{"content":4}\n')
        self.spec['from_start'] = True
        _, messages = read_file(self.spec, initial_cursor(self.spec))
        self.assertEqual([m['content'] for m in messages], ['{bad json}', '{"content":4}'])

    def test_command_changes_and_deduplicated_failures(self):
        command_file = self.root / 'output'
        command_file.write_text('first')
        spec = {'source': 'command', 'command': 'cat output', 'cwd': str(self.root), 'timeout': 1}
        cursor, messages = poll_command(spec, initial_cursor(spec))
        self.assertEqual(messages[0]['content'], 'first')
        self.assertEqual(poll_command(spec, cursor)[1], [])
        command_file.write_text('first second')
        cursor, messages = poll_command(spec, cursor)
        self.assertEqual(messages[0]['content'], 'second')
        spec['command'] = 'printf failed >&2; exit 4'
        cursor, messages = poll_command(spec, cursor)
        self.assertEqual(messages[0]['kind'], 'command_error')
        self.assertEqual(poll_command(spec, cursor)[1], [])
        spec['command'] = 'true'
        self.assertEqual(poll_command(spec, cursor)[1][0]['kind'], 'command_recovered')

    def test_timeout_output_limit_and_cancellation(self):
        start = time.monotonic()
        _, _, failure = run_command('sleep 5', str(self.root), 0.15)
        self.assertIn('timed out', failure)
        self.assertLess(time.monotonic() - start, 2)
        _, _, failure = run_command('yes x', str(self.root), 2)
        self.assertIn('exceeded', failure)
        stop = threading.Event()
        stop.set()
        self.assertIn('cancelled', run_command('sleep 5', str(self.root), 2, stop)[2])

    def test_bounded_notice_is_valid_utf8(self):
        text = bounded('你好🙂' * 10000)
        self.assertLessEqual(len(text.encode()), 8192)
        self.assertIn('truncated', text)


class DurableStore(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cm-db-')
        self.store = Store(self.temp.name)
        self.id = self.store.add({'source': 'file'}, {'offset': 0})

    def tearDown(self):
        self.temp.cleanup()

    def test_checkpoint_and_events_survive_restart(self):
        self.store.checkpoint(self.id, {'offset': 12}, [{'content': 'hello'}])
        reopened = Store(self.temp.name)
        self.assertEqual(reopened.get(self.id)['cursor']['offset'], 12)
        self.assertEqual(reopened.events(self.id, 'pending')[0]['payload']['content'], 'hello')

    def test_inflight_recovery_is_uncertain_not_auto_retried(self):
        self.store.checkpoint(self.id, {}, [{'content': 'hello'}])
        event_id = self.store.events(self.id, 'pending')[0]['id']
        self.store.begin_delivery([event_id])
        self.store.recover()
        self.assertEqual(self.store.events(self.id)[0]['status'], 'uncertain')
        self.store.retry(event_id)
        self.assertEqual(self.store.events(self.id)[0]['status'], 'pending')

    def test_stop_cancels_unsent_events_and_prevents_new_checkpoints(self):
        self.store.checkpoint(self.id, {}, [{'content': 'one'}])
        self.store.stop(self.id)
        self.store.checkpoint(self.id, {}, [{'content': 'two'}])
        self.assertEqual([e['status'] for e in self.store.events(self.id)], ['cancelled'])

    def test_non_private_state_directory_rejected(self):
        path = Path(self.temp.name) / 'public'
        path.mkdir(mode=0o755)
        with self.assertRaisesRegex(ValueError, 'private'):
            Store(path)


class FakeRPC:
    deliveries = []
    blocked = False

    def __init__(self, home, binary, socket=None):
        self.home, self.binary = home, binary

    def target(self, thread_id):
        return {'id': thread_id}

    def deliver(self, thread_id, notice):
        if self.blocked:
            raise Deferred('waiting for user input')
        self.deliveries.append((self.home, thread_id, notice))
        return 'turn-test'

    def close(self):
        pass


class Workers(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cm-worker-')
        self.root = Path(self.temp.name)
        self.path = self.root / 'inbox'
        self.path.write_text('')
        FakeRPC.deliveries = []
        FakeRPC.blocked = False
        self.engine = Engine(self.root / 'state', FakeRPC)
        self.spec = {'source': 'file', 'path': str(self.path), 'interval': 0.2,
                     'codex_home': '/exact/codex-profile', 'codex_bin': '/exact/codex',
                     'thread_id': THREAD}

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def wait_for(self, condition):
        end = time.monotonic() + 3
        while not condition():
            if time.monotonic() > end:
                self.fail('Timed out waiting for worker')
            time.sleep(0.02)

    def test_notification_binds_exact_profile_and_thread(self):
        watcher = self.engine.arm(validate_spec(self.spec))
        self.path.write_text('{"content":"hello"}\n')
        self.wait_for(lambda: len(FakeRPC.deliveries) == 1)
        profile, thread, notice = FakeRPC.deliveries[0]
        self.assertEqual((profile, thread), ('/exact/codex-profile', THREAD))
        self.assertEqual(notice['events'][0]['content'], 'hello')
        self.assertEqual(notice['watcher_id'], watcher['id'])
        self.wait_for(lambda: self.engine.store.events(watcher['id'])[0]['status'] == 'accepted')

    def test_human_wait_defers_and_once_stops_after_acceptance(self):
        FakeRPC.blocked = True
        watcher = self.engine.arm(dict(self.spec, once=True))
        self.path.write_text('hello\n')
        self.wait_for(lambda: bool(self.engine.store.events(watcher['id'])))
        self.assertEqual(FakeRPC.deliveries, [])
        self.assertEqual(self.engine.store.events(watcher['id'])[0]['status'], 'pending')
        FakeRPC.blocked = False
        self.wait_for(lambda: not self.engine.store.get(watcher['id'])['active'])
        self.assertEqual(len(FakeRPC.deliveries), 1)

    def test_unwatch_does_not_deliver_subsequent_appends(self):
        watcher = self.engine.arm(self.spec)
        self.engine.stop_watcher(watcher['id'])
        self.path.write_text('must not deliver\n')
        time.sleep(0.3)
        self.assertEqual(FakeRPC.deliveries, [])

    def test_source_restart_preserves_active_registration_and_offset(self):
        watcher = self.engine.arm(self.spec)
        self.path.write_text('first\n')
        self.wait_for(lambda: len(FakeRPC.deliveries) == 1)
        self.engine.close()
        self.engine = Engine(self.root / 'state', FakeRPC)
        self.engine.start_worker(watcher['id'])
        with self.path.open('a') as stream:
            stream.write('second\n')
        self.wait_for(lambda: len(FakeRPC.deliveries) == 2)
        self.assertEqual(FakeRPC.deliveries[1][2]['events'][0]['content'], 'second')


if __name__ == '__main__':
    unittest.main()
