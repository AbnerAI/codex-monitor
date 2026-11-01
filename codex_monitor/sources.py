"""Bounded file reads and non-overlapping command polls. No model calls."""
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time

MAX_READ = 1024 * 1024
MAX_NOTICE = 8192


def bounded(text, limit=MAX_NOTICE):
    data = text.encode('utf-8')
    if len(data) <= limit:
        return text
    suffix = '\n[truncated; inspect the source for the full output]'
    return data[:limit - len(suffix.encode())].decode('utf-8', errors='ignore') + suffix


def initial_cursor(spec):
    if spec['source'] == 'command':
        return {'output': '', 'error': None}
    try:
        with open(spec['path'], 'rb') as stream:
            info = os.fstat(stream.fileno())
            offset = 0
            if not spec.get('from_start'):
                start = max(0, info.st_size - MAX_READ)
                stream.seek(start)
                tail = stream.read(MAX_READ)
                if tail.endswith(b'\n') or not tail:
                    offset = info.st_size
                else:
                    newline = tail.rfind(b'\n')
                    if newline < 0 and start:
                        raise ValueError('Existing incomplete file record exceeds 1 MiB')
                    offset = start + newline + 1
        return {'device': info.st_dev, 'inode': info.st_ino,
                'offset': offset}
    except FileNotFoundError:
        return {'device': None, 'inode': None, 'offset': 0}


def read_file(spec, cursor):
    try:
        stream = open(spec['path'], 'rb')
    except FileNotFoundError:
        return cursor, []
    with stream:
        info = os.fstat(stream.fileno())
        same = (cursor.get('device'), cursor.get('inode')) == (info.st_dev, info.st_ino)
        offset = cursor.get('offset', 0) if same else 0
        if info.st_size < offset:
            offset = 0
        stream.seek(offset)
        data = stream.read(MAX_READ)
    end = data.rfind(b'\n') + 1
    if not end and len(data) == MAX_READ:
        raise ValueError('A file record exceeds 1 MiB; cursor retained for inspection')
    messages = []
    for line in data[:end].splitlines():
        raw = line.decode('utf-8', errors='replace').strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            record = None
        content = record.get('content') if isinstance(record, dict) else None
        messages.append({'kind': 'file', 'content': bounded(content if isinstance(content, str) else raw)})
    return {'device': info.st_dev, 'inode': info.st_ino, 'offset': offset + end}, messages


def terminate(process, include_exited=False):
    if process.poll() is not None and not include_exited:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=2)
    except ProcessLookupError:
        pass


def run_command(command, cwd, timeout, stopped=None):
    process = subprocess.Popen(command, shell=True, executable='/bin/sh', cwd=cwd,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    output = {'stdout': bytearray(), 'stderr': bytearray()}
    deadline, failure = time.monotonic() + timeout, None
    selector = selectors.DefaultSelector()
    for stream, name in [(process.stdout, 'stdout'), (process.stderr, 'stderr')]:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
    try:
        while selector.get_map():
            if stopped and stopped.is_set():
                failure = 'Command cancelled because the watcher stopped'
                break
            if time.monotonic() >= deadline:
                failure = 'Command timed out after {} seconds'.format(timeout)
                break
            for key, _ in selector.select(timeout=min(0.1, max(0, deadline - time.monotonic()))):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                elif len(output[key.data]) + len(chunk) > MAX_READ:
                    failure = 'Command output exceeded 1 MiB'
                    break
                else:
                    output[key.data].extend(chunk)
            if failure:
                break
        if failure:
            terminate(process, include_exited=True)
        else:
            # Some programs close stdout before exiting; still enforce timeout.
            try:
                process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                failure = 'Command timed out after {} seconds'.format(timeout)
                terminate(process)
        return {name: bytes(data).decode('utf-8', errors='replace') for name, data in output.items()}, process.returncode, failure
    finally:
        terminate(process)
        selector.close()
        process.stdout.close()
        process.stderr.close()


def poll_command(spec, cursor, stopped=None):
    result, code, failure = run_command(spec['command'], spec['cwd'], spec['timeout'], stopped)
    if failure or code:
        error = bounded(failure or 'Command exited {}\n{}'.format(code, result['stderr']))
        messages = [] if error == cursor.get('error') else [{'kind': 'command_error', 'content': error}]
        return dict(cursor, error=error), messages
    current, previous = result['stdout'], cursor.get('output', '')
    delta = current[len(previous):] if current.startswith(previous) else current
    messages = []
    if delta.strip():
        messages.append({'kind': 'command', 'content': bounded(delta.strip())})
    elif cursor.get('error'):
        messages.append({'kind': 'command_recovered', 'content': 'Command succeeded again; output is unchanged or empty'})
    return {'output': current, 'error': None}, messages
