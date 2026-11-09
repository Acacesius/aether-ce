#!/usr/bin/env python3
"""Example local adapter for AETHER Studio (protocol aether.studio.adapter/v1). Standard library only.

It performs no real work: a submitted task is echoed back as a log event. Task forms:
    <text>          echo the text, write each declared output as a text artifact, exit 0
    sleep:<secs>    keep answering heartbeats for <secs>, then exit 0 (cancel ends it early with exit 143)
    fail:<code>     exit with <code> (1..255)
Observe requests (observe:<ref>) report one status event and exit 0.

Studio starts this file inside its sandbox and talks to it over stdin/stdout, one JSON object per line.
See docs/studio/ADAPTER_PROTOCOL.md.
"""
import hashlib
import json
import os
import select
import sys
import time

PROTOCOL = 'aether.studio.adapter/v1'
CAPABILITIES = ['cancel', 'observe', 'submit']


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def sha256(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class Adapter:
    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.buffer = b''
        self.head = None
        self.seq = 0
        self.job_end = None       # (deadline, exit_code) while a sleep task runs

    def send(self, message):
        sys.stdout.write(canonical(message) + '\n')
        sys.stdout.flush()

    def event(self, kind, data, content=None):
        self.seq += 1
        message = {'type': 'event', 'seq': self.seq, 'prev': self.head, 'kind': kind, 'data': data}
        if content is not None:
            message['content'] = content
        self.head = sha256(canonical({'seq': self.seq, 'prev': self.head, 'kind': kind, 'data': data}))
        self.send(message)

    def finish(self, code):
        self.event('result', {'exit_code': code})
        sys.exit(0)

    def receive(self, timeout):
        """Next message, or None when ``timeout`` passes. Raw reads + own buffer, so select() never misses a line
        that a buffered reader already pulled in."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while b'\n' not in self.buffer:
            wait = None if deadline is None else max(0.0, deadline - time.monotonic())
            if not select.select([self.fd], [], [], wait)[0]:
                return None
            chunk = os.read(self.fd, 65536)
            if not chunk:
                sys.exit(0)    # Studio closed the pipe
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b'\n', 1)
        return json.loads(line)

    def submit(self, job):
        self.send({'type': 'accepted', 'ref': f'echo-{job["run_id"]}-{job["stage"]}-{job["attempt"]}'})
        self.event('status', {'state': 'running'})
        task = job['task']
        if task.startswith('fail:'):
            self.finish(max(1, min(255, int(task[5:]))))
        if task.startswith('sleep:'):
            self.job_end = (time.monotonic() + float(task[6:]), 0)
            return
        self.event('log', {'stream': 'stdout', 'text': f'echo: {task}'})
        for name in job['outputs']:
            content = f'echo adapter output "{name}" for task: {task}\n'
            self.event('artifact', {'name': name, 'media_type': 'text/plain', 'sha256': sha256(content),
                                    'size': len(content.encode('utf-8'))}, content)
        self.event('progress', {'done': 1, 'total': 1})
        self.finish(0)

    def run(self):
        hello = self.receive(None)
        assert hello['type'] == 'hello', hello
        self.head = sha256(f'{PROTOCOL}|{hello["session"]}')
        self.send({'type': 'hello', 'protocol': PROTOCOL, 'name': 'echo-adapter', 'capabilities': CAPABILITIES})
        while True:
            timeout = None if self.job_end is None else max(0.0, self.job_end[0] - time.monotonic())
            message = self.receive(timeout)
            if message is None:
                self.event('log', {'stream': 'stdout', 'text': 'sleep finished'})
                self.finish(self.job_end[1])
            kind = message['type']
            if kind in ('refused', 'bye'):
                return
            if kind == 'ping':
                self.send({'type': 'pong', 'n': message['n']})
            elif kind == 'submit':
                self.submit(message['job'])
            elif kind == 'observe':
                self.send({'type': 'accepted', 'ref': message['ref']})
                self.event('status', {'state': 'running'})
                self.finish(0)
            elif kind == 'cancel':
                self.event('log', {'stream': 'stderr', 'text': 'cancel received'})
                self.finish(143)


if __name__ == '__main__':
    Adapter().run()
