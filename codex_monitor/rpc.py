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
    pass


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

    def deliver(self, thread_id, notice):
        with self.lock:
            target = self.target(thread_id)
            status = target.get('status', {})
            if status.get('activeFlags'):
                raise Deferred('Thread is waiting for approval or user input')
            if status.get('type') in ('notLoaded', 'systemError'):
                raise Deferred('Thread is not ready; reopen it in the owning Codex CLI')
            if target.get('canAcceptDirectInput') is False:
                raise Deferred('Owning app-server does not accept input for this thread')
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

