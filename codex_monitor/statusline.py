"""Thread-scoped watcher counts for consumers of a private status snapshot."""
import json
import math
import os
from pathlib import Path
import tempfile
import time


def format_count(count, show_zero=False):
    if type(count) is not int or count < 0:
        raise ValueError('monitor count must be a nonnegative integer')
    if not count and not show_zero:
        return ''
    return '{} {}'.format(count, 'monitor' if count == 1 else 'monitors')


def _scope(thread_id, codex_home):
    if not isinstance(thread_id, str) or not thread_id.strip():
        return None
    try:
        home = os.fspath(codex_home)
        if not isinstance(home, str) or not home.strip() or '\0' in home:
            return None
        return thread_id, str(Path(home).expanduser().resolve())
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _active_scope(watcher):
    if not isinstance(watcher, dict):
        return None
    active = watcher.get('active')
    if type(active) not in (bool, int) or active != 1:
        return None
    spec = watcher.get('spec')
    if not isinstance(spec, dict):
        return None
    return _scope(spec.get('thread_id'), spec.get('codex_home'))


def _unix_time(value):
    if type(value) not in (int, float):
        raise ValueError('Expected a finite, non-negative Unix timestamp')
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError('Expected a finite, non-negative Unix timestamp') from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError('Expected a finite, non-negative Unix timestamp')
    return result


def active_count(watchers, thread_id, codex_home):
    """Count active Store.watchers() entries in one exact thread and profile."""
    scope = _scope(thread_id, codex_home)
    if scope is None:
        return 0
    return sum(_active_scope(watcher) == scope for watcher in watchers)


def build_snapshot(watchers, running=True, now=None):
    """Build schema 1, aggregating active watchers by normalized profile/thread."""
    updated_at = _unix_time(time.time() if now is None else now)
    counts = {}
    for watcher in watchers:
        scope = _active_scope(watcher)
        if scope is not None:
            counts[scope] = counts.get(scope, 0) + 1
    return {
        'schema': 1,
        'running': bool(running),
        'updated_at': updated_at,
        'counts': [
            {'thread_id': thread, 'codex_home': home, 'active': count}
            for (thread, home), count in sorted(counts.items())
        ],
    }


def write_snapshot(state_dir, watchers, running=True, now=None):
    """Atomically replace statusline.json with a mode-0600 snapshot; return its Path."""
    snapshot = build_snapshot(watchers, running=running, now=now)
    directory = Path(state_dir).expanduser().resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / 'statusline.json'
    descriptor, temporary = tempfile.mkstemp(prefix='.statusline-', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(snapshot, stream, ensure_ascii=False, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return target


def read_label(snapshot, thread_id, codex_home, now=None, max_age=5, show_zero=False):
    """Return a count only from valid, running, fresh state for the exact scope.

    show_zero reveals a zero count from healthy state. Invalid or unavailable
    state stays hidden even when show_zero is enabled.
    """
    scope = _scope(thread_id, codex_home)
    if scope is None or not isinstance(snapshot, dict):
        return ''
    if type(snapshot.get('schema')) is not int or snapshot['schema'] != 1:
        return ''
    if snapshot.get('running') is not True:
        return ''
    try:
        current = _unix_time(time.time() if now is None else now)
        updated = _unix_time(snapshot.get('updated_at'))
        age_limit = _unix_time(max_age)
    except ValueError:
        return ''
    if not 0 <= current - updated <= age_limit:
        return ''
    rows = snapshot.get('counts')
    if not isinstance(rows, list):
        return ''
    counts = {}
    for row in rows:
        if not isinstance(row, dict):
            return ''
        row_scope = _scope(row.get('thread_id'), row.get('codex_home'))
        count = row.get('active')
        if row_scope is None or row_scope in counts or type(count) is not int or count < 0:
            return ''
        counts[row_scope] = count
    count = counts.get(scope, 0)
    return format_count(count, show_zero=show_zero)
