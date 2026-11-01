"""Minimal RFC 6455 text client for a private local Unix-domain socket."""
import base64
import hashlib
import json
import os
import socket
import struct
import threading


class UnixWebSocket:
    def __init__(self, path, timeout=15):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.send_lock = threading.Lock()
        self.buffer = bytearray()
        self.closed = False
        try:
            self.socket.settimeout(timeout)
            self.socket.connect(path)
            key = base64.b64encode(os.urandom(16)).decode('ascii')
            request = ('GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n'
                       'Connection: Upgrade\r\nSec-WebSocket-Key: ' + key +
                       '\r\nSec-WebSocket-Version: 13\r\n\r\n')
            self.socket.sendall(request.encode('ascii'))
            while b'\r\n\r\n' not in self.buffer:
                chunk = self.socket.recv(4096)
                if not chunk:
                    raise OSError('Codex closed the WebSocket handshake')
                self.buffer.extend(chunk)
                if len(self.buffer) > 16384:
                    raise OSError('Oversized WebSocket handshake')
            raw, _, remaining = self.buffer.partition(b'\r\n\r\n')
            self.buffer = bytearray(remaining)
            lines = raw.decode('latin1').split('\r\n')
            if len(lines[0].split()) < 2 or lines[0].split()[1] != '101':
                raise OSError('Codex WebSocket handshake rejected: ' + lines[0])
            headers = {k.strip().lower(): v.strip() for k, v in
                       (line.split(':', 1) for line in lines[1:] if ':' in line)}
            expected = base64.b64encode(hashlib.sha1(
                (key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            if headers.get('sec-websocket-accept') != expected:
                raise OSError('Invalid Codex WebSocket handshake accept value')
            # Idle notification subscriptions may stay open indefinitely. RPC
            # deadlines are enforced by the caller, not by partial frame reads.
            self.socket.settimeout(None)
        except Exception:
            self.socket.close()
            raise

    def send_frame(self, data, opcode=1):
        mask = os.urandom(4)
        length = len(data)
        head = bytes([0x80 | opcode])
        if length < 126:
            head += bytes([0x80 | length])
        elif length < 65536:
            head += bytes([0x80 | 126]) + struct.pack('!H', length)
        else:
            head += bytes([0x80 | 127]) + struct.pack('!Q', length)
        masked = bytes(value ^ mask[i % 4] for i, value in enumerate(data))
        with self.send_lock:
            self.socket.sendall(head + mask + masked)

    def send(self, message):
        self.send_frame(json.dumps(message, ensure_ascii=False).encode('utf-8'))

    def read_exact(self, size):
        while len(self.buffer) < size:
            chunk = self.socket.recv(max(4096, size - len(self.buffer)))
            if not chunk:
                raise EOFError('Codex WebSocket closed')
            self.buffer.extend(chunk)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def receive(self):
        fragments = bytearray()
        while not self.closed:
            first, second = self.read_exact(2)
            final, opcode = bool(first & 0x80), first & 0x0f
            size = second & 0x7f
            if size == 126:
                size = struct.unpack('!H', self.read_exact(2))[0]
            elif size == 127:
                size = struct.unpack('!Q', self.read_exact(8))[0]
            if size > 16 * 1024 * 1024 or len(fragments) + size > 16 * 1024 * 1024:
                raise OSError('Codex WebSocket message exceeds 16 MiB')
            mask = self.read_exact(4) if second & 0x80 else None
            data = self.read_exact(size)
            if mask:
                data = bytes(value ^ mask[i % 4] for i, value in enumerate(data))
            if opcode == 8:
                raise EOFError('Codex closed the WebSocket')
            if opcode == 9:
                self.send_frame(data, 10)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1):
                raise OSError('Unexpected binary Codex WebSocket message')
            fragments.extend(data)
            if final:
                return json.loads(fragments.decode('utf-8'))
        raise EOFError('WebSocket is closed')

    def close(self):
        self.closed = True
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()
