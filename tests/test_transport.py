import base64
import hashlib
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest

from codex_monitor.rpc import CodexRPC, Deferred

THREAD = '018f1111-2222-7333-8444-555555555555'


class Peer:
    def __init__(self, status=None):
        self.temp = tempfile.TemporaryDirectory(prefix='cm-ws-')
        self.path = str(Path(self.temp.name) / 's')
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.path)
        self.listener.listen(1)
        self.status = status or {'type': 'idle'}
        self.requests = []
        self.error = None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        try:
            connection, _ = self.listener.accept()
            with connection:
                connection.settimeout(5)
                stream = connection.makefile('rb')
                header = []
                while True:
                    line = stream.readline()
                    if line == b'\r\n':
                        break
                    header.append(line)
                key = next(x.split(b':', 1)[1].strip() for x in header if x.lower().startswith(b'sec-websocket-key:'))
                accept = base64.b64encode(hashlib.sha1(key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
                connection.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
                while True:
                    head = stream.read(2)
                    if not head:
                        break
                    size = head[1] & 127
                    if size == 126:
                        size = struct.unpack('!H', stream.read(2))[0]
                    elif size == 127:
                        size = struct.unpack('!Q', stream.read(8))[0]
                    self.assert_mask = bool(head[1] & 128)
                    mask = stream.read(4)
                    raw = stream.read(size)
                    request = json.loads(bytes(v ^ mask[i % 4] for i, v in enumerate(raw)))
                    self.requests.append(request)
                    if 'id' not in request:
                        continue
                    method = request['method']
                    result = {}
                    if method == 'thread/read':
                        result = {'thread': {'id': THREAD, 'status': self.status, 'canAcceptDirectInput': True}}
                    elif method == 'thread/loaded/list':
                        result = {'data': [THREAD], 'nextCursor': None}
                    elif method == 'turn/start':
                        result = {'turn': {'id': 'turn-accepted', 'status': 'inProgress'}}
                    data = json.dumps({'id': request['id'], 'result': result}).encode()
                    # Exercise fragmented frames (including a control ping).
                    first, rest = data[:7], data[7:]
                    def frame(opcode, data):
                        length = len(data)
                        head = bytes([opcode, length]) if length < 126 else bytes([opcode, 126]) + struct.pack('!H', length)
                        return head + data
                    connection.sendall(frame(1, first))
                    connection.sendall(frame(0x80, rest))
                stream.close()
        except Exception as exc:
            self.error = exc

    def close(self):
        self.listener.close()
        self.thread.join(timeout=2)
        self.temp.cleanup()


class Transport(unittest.TestCase):
    def check_delivery(self, status):
        peer = Peer(status)
        rpc = CodexRPC('/exact/home', '/not-used-by-direct-transport', peer.path, timeout=2)
        try:
            output = {'events': [{'event_id': 'e1', 'content': '你好' * 1000}]}
            turn_id = rpc.deliver(THREAD, output)
            self.assertEqual(turn_id, 'turn-accepted')
            submission = next(r for r in peer.requests if r['method'] == 'turn/start')['params']
            self.assertEqual(submission['input'], [])
            self.assertEqual(submission['threadId'], THREAD)
            self.assertEqual(json.loads(submission['toolOutput']['output']), output)
            self.assertEqual(submission['toolOutput']['namespace'], 'codex_monitor')
            self.assertNotIn('model', submission)
            self.assertNotIn('approvalPolicy', submission)
            self.assertNotIn('sandboxPolicy', submission)
            self.assertTrue(peer.assert_mask)
        finally:
            rpc.close()
            peer.close()
        self.assertIsNone(peer.error)

    def test_idle_turn_starts_with_tool_output(self):
        self.check_delivery({'type': 'idle'})

    def test_active_turn_uses_the_same_standalone_output_route(self):
        self.check_delivery({'type': 'active', 'activeFlags': []})

    def test_human_wait_does_not_submit(self):
        peer = Peer({'type': 'active', 'activeFlags': ['waitingOnApproval']})
        rpc = CodexRPC('/exact/home', '/codex', peer.path, timeout=2)
        try:
            with self.assertRaises(Deferred):
                rpc.deliver(THREAD, {'events': []})
            self.assertFalse(any(r['method'] == 'turn/start' for r in peer.requests))
        finally:
            rpc.close()
            peer.close()

