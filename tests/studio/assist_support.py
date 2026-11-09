"""Fake local chat endpoint (127.0.0.1 only, no real model) for AHS-06 tests."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

from aether.studio import schema

GOOD = {'name': 'Hello harness', 'description': 'says hello',
        'spec': {'mission': 'DEMO: say hello', 'executor': {'kind': 'mock', 'ref': 'mock:demo'},
                 'capabilities': ['demo.run'], 'limits': {'wall_seconds': 60},
                 'stages': [{'key': 'hello', 'title': 'Hello', 'executor_step': 'mock:success', 'timeout_seconds': 30,
                             'capabilities': ['demo.run']}]}}


def good(**spec_changes):
    value = json.loads(json.dumps(GOOD))
    value['spec'].update(spec_changes)
    return value


class FakeModel:
    """replies: list of str | ('redirect', url) | ('status', code) | ('big', n); the last one repeats."""

    def __init__(self, replies, style='openai'):
        self.replies, self.style, self.requests = list(replies), style, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.requests.append({'body': body, 'headers': dict(self.headers), 'path': self.path})
                reply = outer.replies[min(len(outer.requests), len(outer.replies)) - 1]
                if isinstance(reply, tuple) and reply[0] == 'redirect':
                    self.send_response(302)
                    self.send_header('Location', reply[1])
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                if isinstance(reply, tuple) and reply[0] == 'status':
                    self.send_response(reply[1])
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                content = 'x' * reply[1] if isinstance(reply, tuple) else (reply if isinstance(reply, str) else json.dumps(reply))
                payload = {'choices': [{'message': {'role': 'assistant', 'content': content}}]} if outer.style == 'openai' \
                    else {'message': {'role': 'assistant', 'content': content}, 'done': True}
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                return

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.port}/v1/chat/completions' if self.style == 'openai' else f'http://127.0.0.1:{self.port}/api/chat'

    def env(self, url=None, model='test-model'):
        return {'AETHER_STUDIO_ASSIST_URL': url or self.url, 'AETHER_STUDIO_ASSIST_MODEL': model}

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def fake(test, replies, **kw):
    model = FakeModel(replies, **kw)
    test.addCleanup(model.close)
    return model


def prompt_text(request):
    return '\n'.join(m['content'] for m in request['body']['messages'])
