"""Transactional source checkpoints and a durable notification outbox."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
import uuid


class Store:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.stat().st_mode & 0o077:
            raise ValueError('State directory must be private (chmod 700): ' + str(self.root))
        self.path = self.root / 'state.sqlite3'
        with self.connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS watchers (
                    id TEXT PRIMARY KEY, spec TEXT NOT NULL, cursor TEXT NOT NULL,
                    active INTEGER NOT NULL, created REAL NOT NULL, last_error TEXT);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    watcher_id TEXT NOT NULL, payload TEXT NOT NULL,
                    status TEXT NOT NULL, created REAL NOT NULL,
                    turn_id TEXT, error TEXT);
                CREATE INDEX IF NOT EXISTS events_pending ON events(watcher_id,status,seq);
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, spec, cursor):
        watcher_id = 'monitor-' + uuid.uuid4().hex[:12]
        with self.connection() as db:
            db.execute('INSERT INTO watchers VALUES(?,?,?,?,?,?)',
                       (watcher_id, json.dumps(spec), json.dumps(cursor), 1, time.time(), None))
        return watcher_id

    def watchers(self, active_only=False):
        with self.connection() as db:
            rows = db.execute('SELECT * FROM watchers' + (' WHERE active=1' if active_only else '') +
                              ' ORDER BY created').fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value['spec'], value['cursor'] = json.loads(value['spec']), json.loads(value['cursor'])
            result.append(value)
        return result

    def get(self, watcher_id):
        return next((w for w in self.watchers() if w['id'] == watcher_id), None)

    def stop(self, watcher_id):
        with self.connection() as db:
            row = db.execute('SELECT id FROM watchers WHERE id=?', (watcher_id,)).fetchone()
            if not row:
                raise ValueError('Unknown watcher: ' + watcher_id)
            db.execute('UPDATE watchers SET active=0 WHERE id=?', (watcher_id,))
            db.execute("UPDATE events SET status='cancelled' WHERE watcher_id=? AND status='pending'",
                       (watcher_id,))

    def checkpoint(self, watcher_id, cursor, messages):
        # A crash cannot advance the read cursor without saving its events.
        with self.connection() as db:
            active = db.execute('SELECT active FROM watchers WHERE id=?', (watcher_id,)).fetchone()
            if not active or not active[0]:
                return
            for message in messages:
                db.execute('INSERT INTO events(id,watcher_id,payload,status,created) VALUES(?,?,?,?,?)',
                           ('event-' + uuid.uuid4().hex, watcher_id, json.dumps(message), 'pending', time.time()))
            db.execute('UPDATE watchers SET cursor=?,last_error=NULL WHERE id=?',
                       (json.dumps(cursor), watcher_id))

    def error(self, watcher_id, error):
        with self.connection() as db:
            db.execute('UPDATE watchers SET last_error=? WHERE id=?', (str(error)[:1000], watcher_id))

    def events(self, watcher_id=None, status=None, limit=50):
        query, values, clauses = 'SELECT * FROM events', [], []
        for column, value in [('watcher_id', watcher_id), ('status', status)]:
            if value is not None:
                clauses.append(column + '=?')
                values.append(value)
        if clauses:
            query += ' WHERE ' + ' AND '.join(clauses)
        query += ' ORDER BY seq ' + ('ASC' if status == 'pending' else 'DESC') + ' LIMIT ?'
        values.append(limit)
        with self.connection() as db:
            rows = db.execute(query, values).fetchall()
        return [dict(row, payload=json.loads(row['payload'])) for row in rows]

    def begin_delivery(self, event_ids):
        with self.connection() as db:
            for event_id in event_ids:
                db.execute("UPDATE events SET status='sending' WHERE id=? AND status='pending'", (event_id,))

    def finish(self, event_ids, status, turn_id=None, error=None):
        with self.connection() as db:
            for event_id in event_ids:
                db.execute('UPDATE events SET status=?,turn_id=?,error=? WHERE id=?',
                           (status, turn_id, str(error)[:1000] if error else None, event_id))

    def recover(self):
        with self.connection() as db:
            db.execute("UPDATE events SET status='uncertain',error='Service stopped during submission; verify history before retry' WHERE status='sending'")

    def retry(self, event_id):
        with self.connection() as db:
            row = db.execute('SELECT watcher_id,status FROM events WHERE id=?', (event_id,)).fetchone()
            if not row or row['status'] not in ('uncertain', 'failed'):
                raise ValueError('Only an uncertain or failed event can be retried')
            if not db.execute('SELECT active FROM watchers WHERE id=?', (row['watcher_id'],)).fetchone()[0]:
                raise ValueError('Watcher is stopped')
            db.execute("UPDATE events SET status='pending',error=NULL WHERE id=?", (event_id,))

