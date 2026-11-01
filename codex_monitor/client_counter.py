"""Resolve a configured native client title against active watcher scopes.

The native thread-id title item is an explicit identity field, but Codex 0.166
renders only its first 29 characters followed by ``...``. Resolution therefore
requires exactly one matching full thread/profile pair in the active registry.
"""
import re
from pathlib import Path

from .statusline import format_count


TITLE_ITEMS = ['app-name', 'thread-id', 'app-name', 'thread-name', 'project-name']

_UUID_TEXT = r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}'
_PREFIX_TEXT = r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{5}'
_UUID = re.compile(_UUID_TEXT + r'\Z')
_NATIVE_TITLE = re.compile(
    r'\Acodex \| (?P<identity>' + _UUID_TEXT + '|' + _PREFIX_TEXT + r'\.\.\.)'
    r' \| codex(?: \| [^\r\n]+)?\Z')


def native_thread_prefix(title):
    """Read only the configured first ``codex | thread-id | codex`` fields.

    The returned value is either the full UUID or the native 29-character
    prefix, without its ellipsis. No other title field is searched for an ID.
    Both fixed markers are required so an omitted native ID cannot shift a
    UUID-shaped thread name or project name into the identity position.
    """
    if not isinstance(title, str) or title != title.strip():
        return None
    match = _NATIVE_TITLE.fullmatch(title)
    if match is None:
        return None
    return match.group('identity').removesuffix('...').lower()


def _active_scope(row):
    if not isinstance(row, dict):
        return None
    active = row.get('active')
    if type(active) not in (bool, int) or active != 1:
        return None
    # The service list is flat; Store.watchers() entries carry the same fields
    # in spec. Both represent registry data, never a GUI-derived scope.
    spec = row.get('spec', row)
    if not isinstance(spec, dict):
        return None
    thread = spec.get('thread_id')
    home = spec.get('codex_home')
    if not isinstance(thread, str) or _UUID.fullmatch(thread) is None:
        return None
    if not isinstance(home, str) or not home.strip() or '\0' in home:
        return None
    try:
        path = Path(home).expanduser()
        if not path.is_absolute():
            return None
        normalized_home = str(path.resolve())
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return thread.lower(), normalized_home


def _unbound(state, prefix, active=None):
    return {'state': state, 'thread_id': None, 'codex_home': None,
            'active': active, 'label': '', 'native_prefix': prefix}


def resolve_counter(title, rows, codex_home=None):
    """Bind only one active full thread/home pair matching the native field.

    Duplicate watchers in that pair increase the count. Multiple full thread
    IDs or normalized homes are ambiguous, including when the title has a full
    UUID. Stopped and malformed registry rows do not contribute a scope.
    """
    prefix = native_thread_prefix(title)
    if prefix is None:
        return _unbound('awaiting_client_identity', None)
    expected_home = None
    if codex_home is not None:
        try:
            path = Path(codex_home).expanduser()
            if not path.is_absolute():
                return _unbound('unavailable_client_profile', prefix)
            expected_home = str(path.resolve())
        except (OSError, RuntimeError, TypeError, ValueError):
            return _unbound('unavailable_client_profile', prefix)
    counts = {}
    try:
        iterator = iter(rows)
    except TypeError:
        iterator = iter(())
    for row in iterator:
        scope = _active_scope(row)
        if (scope is not None and scope[0].startswith(prefix)
                and (expected_home is None or scope[1] == expected_home)):
            counts[scope] = counts.get(scope, 0) + 1
    if not counts:
        return _unbound('no_active_watchers', prefix, active=0)
    if len(counts) != 1:
        return _unbound('ambiguous_client_identity', prefix)
    (thread, home), count = next(iter(counts.items()))
    return {'state': 'bound', 'thread_id': thread, 'codex_home': home,
            'active': count, 'label': format_count(count), 'native_prefix': prefix}
