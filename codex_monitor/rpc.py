"""JSON-RPC over Codex's supported Unix WebSocket control transport."""
import collections
import json
import os
from pathlib import Path
import shutil
import threading
import time

from .websocket import UnixWebSocket
from . import __version__


class RpcError(RuntimeError):
    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or {}


class UncertainDelivery(RpcError):
    """A mutating request was sent, but its acknowledgement was lost."""


class Deferred(RpcError):
    """The target is currently waiting for human input, or cannot accept input."""


def codex_binary(explicit=None):
    candidate = explicit or os.environ.get('CODEX_MONITOR_CODEX_BIN')
    if not candidate and os.environ.get('CODEX_MANAGED_PACKAGE_ROOT'):
        managed = Path(os.environ['CODEX_MANAGED_PACKAGE_ROOT']) / 'bin/codex.js'
        if managed.is_file():
            candidate = str(managed)
    candidate = candidate or shutil.which('codex')
    if not candidate:
        raise RpcError('Codex CLI not found; set CODEX_MONITOR_CODEX_BIN or --codex-bin')
    located = shutil.which(candidate) if '/' not in candidate else candidate
    if not located or not os.access(located, os.X_OK):
        raise RpcError('Codex executable is unavailable: ' + candidate)
    return str(Path(located).absolute())


def home_path(value=None):
    return str(Path(value or os.environ.get('CODEX_HOME', '~/.codex')).expanduser().resolve())


class CodexRPC:
    def __init__(self, home, binary, socket_path=None, timeout=15, on_notification=None):
        self.home, self.binary = home, binary
        self.socket_path = socket_path or str(Path(home) / 'app-server-control/app-server-control.sock')
        self.timeout = timeout
        self.on_notification = on_notification
        self.connection = None
        self.sequence = 0
        self.responses = {}
        self.condition = threading.Condition()
        self.lock = threading.RLock()
        self.stderr = collections.deque(maxlen=8)
        self.diagnostics = collections.deque(maxlen=20)
        self.eof = False

    def connect(self):
        with self.lock:
            if self.connection and not self.eof:
                return
            self.close()
            self.connection = UnixWebSocket(self.socket_path, timeout=self.timeout)
            self.responses, self.eof = {}, False
            threading.Thread(target=self._read, args=(self.connection,), daemon=True).start()
            self._request('initialize', {
                'clientInfo': {'name': 'codex-monitor', 'title': 'Codex Monitor', 'version': __version__},
                'capabilities': {'experimentalApi': True}}, False)
            self.connection.send({'method': 'initialized'})

    def _read(self, connection):
        try:
            while not connection.closed:
                message = connection.receive()
                # Never approve a server request or impersonate a user. Approval
                # requests remain owned by the thread's attached Codex client.
                if 'id' in message and ('result' in message or 'error' in message):
                    with self.condition:
                        self.responses[message['id']] = message
                        self.condition.notify_all()
                elif message.get('method'):
                    if self.on_notification:
                        self.on_notification(message)
                    params = message.get('params', {})
                    if 'id' in message or message['method'] in ('error', 'turn/completed'):
                        self.diagnostics.append({'method': message['method'],
                                                 'threadId': params.get('threadId'),
                                                 'error': params.get('error'),
                                                 'turn_status': params.get('turn', {}).get('status')})
        except (OSError, EOFError, ValueError) as exc:
            self.stderr.append(str(exc)[:500])
        finally:
            if self.connection is connection:
                with self.condition:
                    self.eof = True
                    self.condition.notify_all()

    def _request(self, method, params, mutating):
        self.sequence += 1
        request_id = self.sequence
        request = {'id': request_id, 'method': method, 'params': params}
        try:
            self.connection.send(request)
        except (OSError, ValueError) as exc:
            if mutating:
                raise UncertainDelivery('Connection broke while submitting an event') from exc
            raise RpcError('Codex control connection closed') from exc
        deadline = time.monotonic() + self.timeout
        with self.condition:
            while request_id not in self.responses:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self.eof:
                    kind = UncertainDelivery if mutating else RpcError
                    raise kind('No acknowledgement for ' + method + '; transport: ' +
                               (' | '.join(self.stderr) or '(empty)'))
                self.condition.wait(remaining)
            response = self.responses.pop(request_id)
        if 'error' in response:
            error = response['error']
            raise RpcError('{}: {}'.format(method, error.get('message', str(error))))
        return response.get('result', {})

    def request(self, method, params, mutating=False):
        with self.lock:
            self.connect()
            return self._request(method, params, mutating)

    def target(self, thread_id):
        return self.request('thread/read', {'threadId': thread_id, 'includeTurns': False})['thread']

    def runtime_loaded(self, thread_id):
        """Check this server's runtime, not the history shared on disk."""
        cursor, seen = None, set()
        for _ in range(32):
            params = {'limit': 100}
            if cursor is not None:
                params['cursor'] = cursor
            reply = self.request('thread/loaded/list', params)
            if (not isinstance(reply, dict) or not isinstance(reply.get('data'), list)
                    or not all(isinstance(item, str) for item in reply['data'])):
                raise RpcError('Invalid thread/loaded/list response; runtime ownership cannot be verified')
            if thread_id in reply['data']:
                return True
            cursor = reply.get('nextCursor')
            if cursor is None:
                return False
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise RpcError('Invalid thread/loaded/list cursor; runtime ownership cannot be verified')
            seen.add(cursor)
        raise RpcError('thread/loaded/list exceeded its page limit; runtime ownership cannot be verified')

    def readiness(self, thread_id):
        """Distinguish persisted identity from a runtime that can accept input."""
        target = self.target(thread_id)
        status = target.get('status')
        status = status if isinstance(status, dict) else {}
        result = {'exact_thread_verified': target.get('id') == thread_id,
                  'thread_status': status,
                  'can_accept_direct_input': target.get('canAcceptDirectInput'),
                  'runtime_loaded': None, 'delivery_ready': False,
                  'registration_ready': False, 'delivery_state': 'thread_id_mismatch'}
        if not result['exact_thread_verified']:
            return result
        try:
            result['runtime_loaded'] = self.runtime_loaded(thread_id)
        except (RpcError, OSError, ValueError) as exc:
            result.update(delivery_state='runtime_verification_failed', diagnostic_error=str(exc))
            return result
        state = status.get('type')
        if not result['runtime_loaded']:
            result['delivery_state'] = 'thread_not_loaded'
        elif state == 'notLoaded':
            result['delivery_state'] = 'thread_state_inconsistent'
        elif state == 'systemError':
            result['delivery_state'] = 'system_error'
        elif state not in ('idle', 'active'):
            result['delivery_state'] = 'unsupported_thread_status'
        elif result['can_accept_direct_input'] is False:
            result['delivery_state'] = 'direct_input_not_supported'
        else:
            result['registration_ready'] = True
            result['delivery_ready'] = not bool(status.get('activeFlags'))
            result['delivery_state'] = ('ready' if result['delivery_ready']
                                        else 'waiting_for_approval_or_input')
        return result

    def unavailable_message(self, readiness):
        state = readiness['delivery_state']
        if state == 'thread_not_loaded':
            return ('Target thread is not loaded in the connected app-server. Shared history '
                    'does not prove runtime ownership: an in-process Codex CLI or another '
                    'runtime cannot receive notifications through this daemon. Verify the '
                    'owning shared-daemon connection; do not resume the thread in this server.')
        return 'Target runtime cannot accept monitor delivery: ' + state

    def deliver(self, thread_id, notice):
        with self.lock:
            readiness = self.readiness(thread_id)
            if not readiness['delivery_ready']:
                message = ('Thread is waiting for approval or user input'
                           if readiness['delivery_state'] == 'waiting_for_approval_or_input'
                           else self.unavailable_message(readiness))
                raise Deferred(message, readiness)
            # Standalone tool output preserves external-event provenance. Omit
            # model, permissions, cwd and approval overrides entirely.
            result = self.request('turn/start', {
                'threadId': thread_id, 'input': [], 'turnTrigger': 'codex-monitor',
                'toolOutput': {'namespace': 'codex_monitor', 'name': 'monitor_event',
                               'output': json.dumps(notice, ensure_ascii=False)}}, mutating=True)
            turn = result.get('turn', {})
            if not turn.get('id'):
                raise UncertainDelivery('Submission returned no turn id')
            return turn['id']

    def close(self):
        connection, self.connection = self.connection, None
        if connection:
            connection.close()

