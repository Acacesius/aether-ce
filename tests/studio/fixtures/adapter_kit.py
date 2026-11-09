"""Minimal third-party side of aether.studio.adapter/v1 for test fixtures (stdlib only; runs inside the sandbox)."""
import hashlib
import json
import os
import select
import sys
import time

PROTOCOL = 'aether.studio.adapter/v1'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def sha256(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class Peer:
    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.buffer = b''
        self.head = None
        self.seq = 0
        self.session = None

    def raw(self, data: bytes):
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    def send(self, message):
        self.raw((canonical(message) + '\n').encode('utf-8'))

    def receive(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while b'\n' not in self.buffer:
            wait = None if deadline is None else max(0.0, deadline - time.monotonic())
            if not select.select([self.fd], [], [], wait)[0]:
                return None
            chunk = os.read(self.fd, 65536)
            if not chunk:
                sys.exit(0)
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b'\n', 1)
        return json.loads(line)

    def handshake(self, capabilities=('observe', 'submit', 'cancel'), protocol=PROTOCOL, name='fixture'):
        hello = self.receive()
        self.session = hello['session']
        self.head = sha256(f'{PROTOCOL}|{self.session}')
        self.send({'type': 'hello', 'protocol': protocol, 'name': name, 'capabilities': list(capabilities)})
        return self.receive()   # welcome or refused

    def next_request(self):
        """Answer pings until a submit/observe/cancel/bye arrives."""
        while True:
            message = self.receive()
            if message['type'] == 'ping':
                self.send({'type': 'pong', 'n': message['n']})
            else:
                return message

    def event(self, kind, data, content=None, *, seq=None, prev=None):
        self.seq = self.seq + 1 if seq is None else seq
        prev = self.head if prev is None else prev
        message = {'type': 'event', 'seq': self.seq, 'prev': prev, 'kind': kind, 'data': data}
        if content is not None:
            message['content'] = content
        self.head = sha256(canonical({'seq': self.seq, 'prev': prev, 'kind': kind, 'data': data}))
        self.send(message)

    def log(self, text):
        self.event('log', {'stream': 'stdout', 'text': text})

    def serve_pings(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            message = self.receive(max(0.0, end - time.monotonic()))
            if message and message['type'] == 'ping':
                self.send({'type': 'pong', 'n': message['n']})
            elif message and message['type'] in ('cancel', 'bye'):
                return message
        return None
