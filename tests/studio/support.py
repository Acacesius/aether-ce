"""Shared fixtures for Studio CE tests (network-free; loopback HTTP only in API tests)."""
from __future__ import annotations

import collections
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time

from aether.studio import schema
from aether.studio.executors.mock import MockExecutor
from aether.studio.mode import Mode
from aether.studio.runner import Runner
from aether.studio.store import Store

FAST = dict(heartbeat_seconds=0.1, lease_seconds=3.0, poll_seconds=0.01, tick_seconds=0.02)


class CountingMock(MockExecutor):
    """MockExecutor that records starts and cancels (thread-safe)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.starts = collections.Counter()
        self.cancels = []

    def start(self, run, stage, emit):
        with self.lock:
            self.starts[run.id] += 1
        return super().start(run, stage, emit)

    def cancel(self, handle, grace_seconds):
        with self.lock:
            self.cancels.append((handle.run_id, handle.stage_key, grace_seconds))
        return super().cancel(handle, grace_seconds)


def tempdir(test):
    tmp = tempfile.TemporaryDirectory(prefix='studio-test-')
    test.addCleanup(tmp.cleanup)
    return Path(tmp.name)


def store(test, path=None, **kw):
    path = path or tempdir(test) / 'studio.sqlite3'
    s = Store(path, **kw)
    test.addCleanup(s.close)
    return s


def runner(test, s, *, executor=None, max_workers=1, start=True, **kw):
    options = dict(FAST)
    options.update(kw)
    r = Runner(s, {'mock': executor or MockExecutor()}, max_workers=max_workers, **options)
    if start:
        r.start()
    test.addCleanup(r.stop)
    return r


def submit(s, step='mock:success', *, key=None, spec=None, overrides=None, name='test', **spec_kw):
    spec = schema.validate_spec(spec or schema.demo_spec(step, **spec_kw))
    version_id = s.ensure_inline_version(name, spec)
    snapshot = schema.apply_overrides(spec, overrides or {})
    run_id, _ = s.submit(version_id, snapshot, overrides or {}, idempotency_key=key or schema.new_id('run'),
                         request_sha256=schema.sha256_hex(schema.canonical_json(spec)))
    return run_id


def wait_for(fn, timeout=10.0, step=0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = fn()
        if value:
            return value
        time.sleep(step)
    raise AssertionError('timed out waiting for condition')


def wait_status(s, run_id, *statuses, timeout=10.0):
    return wait_for(lambda: (lambda r: r if r['status'] in statuses else None)(s.get_run(run_id)), timeout)


def ce_mode(data_dir, provider='none'):
    return Mode('custom', provider, 'docker', Path(data_dir))


class Client:
    def __init__(self, port, token):
        self.port = port
        self.token = token
        self.origin = f'http://127.0.0.1:{port}'

    def request(self, method, path, body=None, *, auth=True, headers=None):
        values = {'Host': f'127.0.0.1:{self.port}'}
        if auth:
            values['Authorization'] = 'Bearer ' + self.token
        if method in ('POST', 'PUT'):
            values['Origin'] = self.origin
            values['Content-Type'] = 'application/json'
        values.update(headers or {})
        payload = body if isinstance(body, (bytes, str)) or body is None else json.dumps(body)
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        conn.request(method, path, body=payload, headers=values)
        response = conn.getresponse()
        status, data = response.status, response.read()
        conn.close()
        return status, (json.loads(data) if data else None)


def serve(test, assist_env=None, assist_resolver=None, **runner_kw):
    """Start a real StudioServer on 127.0.0.1:0 with a temp CE data dir. Returns (app, client)."""
    from aether.studio.app import StudioServer, build
    data = tempdir(test)
    os.chmod(data, 0o700)
    options = dict(FAST)
    options.update(runner_kw)
    if assist_resolver:
        options['assist_resolver'] = assist_resolver
    app = build(ce_mode(data), assist_env=assist_env or {}, **options)
    httpd = StudioServer(('127.0.0.1', 0), app)
    app.runner.start()
    thread = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
    thread.start()

    def close():
        httpd.shutdown()
        httpd.server_close()
        app.runner.stop()
        app.store.close()
    test.addCleanup(close)
    token = (data / 'owner.token').read_text().strip()
    return app, Client(httpd.server_address[1], token)


# ---------------------------------------------------------------- adapters (AHS-04)
FIXTURES = Path(__file__).resolve().parent / 'fixtures'
EXAMPLES = Path(__file__).resolve().parents[2] / 'examples' / 'adapters'
ADAPTER_FAST = dict(heartbeat_seconds=0.2, dead_seconds=1.5, probe_ttl=30.0, handshake_seconds=5.0)


def install_adapters(test):
    """Copy the example and fixture adapters into a fresh 0700 directory outside any data dir (umask-independent)."""
    import shutil
    target = tempdir(test) / 'adapters'
    target.mkdir(mode=0o700)
    for source in (EXAMPLES / 'echo_adapter.py', *FIXTURES.glob('*.py')):
        shutil.copyfile(source, target / source.name)
        os.chmod(target / source.name, 0o600)
    return target


def adapter_env(test, **kw):
    """(store, AdapterExecutor, data_dir, adapters_dir) with fast heartbeats."""
    from aether.studio.executors.adapter import AdapterExecutor
    data = tempdir(test)
    s = store(test, data / 'studio.sqlite3')
    options = dict(ADAPTER_FAST)
    options.update(kw)
    return s, AdapterExecutor(s, data_dir=data, **options), data, install_adapters(test)


def enroll(s, data_dir, script, *, name='echo', args=(), scopes=('observe', 'submit', 'cancel')):
    from aether.studio.executors.adapter import validate_enrollment
    item = validate_enrollment({'name': name, 'script': str(script), 'args': list(args)}, data_dir)
    adapter = s.enroll_adapter(item['name'], item['script'], item['args'], item['sha256'])
    return s.grant_adapter(adapter['id'], list(scopes)) if scopes else adapter


def adapter_spec(step='submit:hello', *, ref='echo', outputs=(), wall_seconds=60, timeout_seconds=30, grace=2,
                 extra_caps=()):
    op = step.split(':', 1)[0]
    stage_caps = ['adapter.' + op] + (['artifact.write'] if outputs else []) + list(extra_caps)
    caps = sorted(set(['adapter.observe'] + stage_caps))
    return {'mission': 'adapter test', 'executor': {'kind': 'adapter', 'ref': ref}, 'capabilities': caps,
            'limits': {'wall_seconds': wall_seconds}, 'cancellation': {'grace_seconds': grace},
            'stages': [{'key': 'work', 'title': 'Work', 'executor_step': step, 'timeout_seconds': timeout_seconds,
                        'capabilities': stage_caps, 'outputs': list(outputs)}],
            'evidence': {'outputs': list(outputs), 'require_tests': False}}


def adapter_runner(test, s, executor, **kw):
    options = dict(FAST)
    options.update(kw)
    r = Runner(s, {'mock': MockExecutor(), 'adapter': executor}, **options)
    r.start()
    test.addCleanup(r.stop)
    return r


def all_events(s, run_id):
    return s.events(run_id, 0, 10_000)[0]
