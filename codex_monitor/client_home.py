"""Discover the original Codex entry's home without creating a thread or turn.

Use its existing daemon when available. A cold launch uses a temporary stdio
app-server for initialize only; that process group is stopped after the reply.
No account wrapper is inspected or reimplemented, and no daemon is started.
"""
import contextlib
import json
import math
import os
from pathlib import Path
import select
import signal
import subprocess
import threading
import time

from .websocket import UnixWebSocket
from . import __version__


_REQUEST = {'id': 'codex-monitor-home', 'method': 'initialize', 'params': {
    'clientInfo': {'name': 'codex-monitor-home', 'version': __version__}}}
_VALUE_OPTIONS = {'-m', '--model', '--local-provider', '-s', '--sandbox', '-C', '--cd',
                  '--add-dir', '-a', '--ask-for-approval', '-i', '--image',
                  '--enable', '--disable', '--remote', '--remote-auth-token-env'}


def context_arguments(arguments):
    """Carry config/profile arguments as argv, stopping at the literal boundary."""
    result = []
    values = iter(arguments)
    for value in values:
        if value == '--':
            break
        if value in ('-c', '--config', '-p', '--profile'):
            try:
                argument = next(values)
            except StopIteration:
                raise ValueError('Missing Codex config/profile argument') from None
            result.extend((value, argument))
        elif value.startswith(('--config=', '--profile=')) or (
                value.startswith(('-c', '-p')) and len(value) > 2 and not value.startswith('--')):
            result.append(value)
        elif value in _VALUE_OPTIONS:
            next(values, None)
    return result


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError('Codex home discovery timed out')
    return remaining


def _home(value):
    if not isinstance(value, str) or not value or '\0' in value or not Path(value).is_absolute():
        raise ValueError('Codex initialize did not report an absolute codexHome')
    return str(Path(value).resolve())


def _stop(process):
    # Only this newly created process group is targeted, including node shims.
    # A wrapper leader may have exited while its spawned CLI is still alive.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=0.2)
    for stream in (process.stdin, process.stdout):
        if stream is not None:
            stream.close()


def _spawn(command):
    return subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, start_new_session=True)


def _version(command, deadline):
    process = _spawn(command + ['app-server', 'daemon', 'version'])
    try:
        output, _ = process.communicate(input=b'', timeout=_remaining(deadline))
        if process.returncode:
            return None
        value = json.loads(output)
        return value if isinstance(value, dict) else None
    except (ValueError, subprocess.TimeoutExpired):
        return None
    finally:
        _stop(process)


def _socket_home(path, deadline):
    # Allocate before initializing so the wall-clock watchdog can close even
    # a socket stalled inside the WebSocket upgrade, not just later RPC reads.
    connection = UnixWebSocket.__new__(UnixWebSocket)

    def expire():
        with contextlib.suppress(Exception):
            connection.close()

    timer = threading.Timer(_remaining(deadline), expire)
    timer.daemon = True
    timer.start()
    try:
        UnixWebSocket.__init__(connection, path, timeout=_remaining(deadline))
        connection.send(_REQUEST)
        while True:
            _remaining(deadline)
            response = connection.receive()
            if response.get('id') == _REQUEST['id']:
                if 'error' in response:
                    raise RuntimeError('Codex initialize was rejected')
                return _home(response.get('result', {}).get('codexHome'))
    finally:
        timer.cancel()
        expire()


def _stdio_home(command, deadline):
    process = _spawn(command + ['app-server'])
    try:
        process.stdin.write((json.dumps(_REQUEST) + '\n').encode())
        process.stdin.flush()  # Keep stdin open until initialize has replied.
        pending = bytearray()
        total = 0
        while True:
            ready, _, _ = select.select([process.stdout], [], [], _remaining(deadline))
            if not ready:
                raise RuntimeError('Codex home discovery timed out')
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError('Codex closed before home discovery completed')
            total += len(chunk)
            if total > 1024 * 1024:
                raise RuntimeError('Codex home discovery response exceeded its limit')
            pending.extend(chunk)
            while b'\n' in pending:
                line, _, tail = pending.partition(b'\n')
                pending = bytearray(tail)
                response = json.loads(line)
                if isinstance(response, dict) and response.get('id') == _REQUEST['id']:
                    if 'error' in response:
                        raise RuntimeError('Codex initialize was rejected')
                    return _home(response.get('result', {}).get('codexHome'))
    finally:
        _stop(process)


def discover_client_home(executable, arguments, timeout=5.0):
    """Return authoritative normalized codexHome selected by the original entry.

    All discovery attempts share one deadline (at most five seconds), with a
    small cleanup reserve. Failures expose no child stderr or authentication data.
    """
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 5:
        raise ValueError('Discovery timeout must be positive and at most five seconds')
    if not isinstance(executable, str) or not executable or '\0' in executable:
        raise ValueError('An original Codex executable is required')
    deadline = time.monotonic() + max(0, timeout - 0.2)
    command = [executable] + context_arguments(arguments)
    try:
        version = _version(command, deadline)
        if version is not None:
            if 'codexHome' in version:
                return _home(version['codexHome'])
            path = version.get('socketPath')
            if isinstance(path, str) and path and '\0' not in path and Path(path).is_absolute():
                try:
                    return _socket_home(path, deadline)
                except (OSError, EOFError, ValueError, RuntimeError):
                    pass
        return _stdio_home(command, deadline)
    except (OSError, EOFError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        raise RuntimeError('Could not establish the original Codex client home') from None
