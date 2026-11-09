"""Deterministic orchestration harness: fake monotonic clock + scripted executor; no threads, no sleeping."""
from __future__ import annotations

import collections
import threading

from aether.studio import schema
from aether.studio.executors.base import Handle
from aether.studio.orchestrator import RunDriver
from aether.studio.runner import Runner
from tests.studio import support

OWNER = 'owner-a'


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def stage(key, needs=(), step='mock:success', **extra):
    item = {'key': key, 'title': key, 'executor_step': step, 'timeout_seconds': 60, 'capabilities': ['demo.run'],
            'needs': list(needs)}
    item.update(extra)
    return item


class FakeExecutor:
    """Behaviour per stage key (dict, or list indexed by attempt-1; the last entry repeats).

    keys: exit (default 0), ticks (polls reporting running before exit), hang, stubborn (ignores cancel),
    events [(type, data)] emitted at start, block (capability -> reports blocked unless approved)."""
    kind = 'mock'
    demo = True

    def __init__(self, script=None):
        self.script = script or {}
        self.executions = collections.Counter()
        self.views = []
        self.cancels = []
        self.live = 0
        self.max_live = 0
        self.lock = threading.Lock()

    def behaviour(self, key, attempt):
        b = self.script.get(key, {})
        return b[min(attempt, len(b)) - 1] if isinstance(b, list) else b

    def capabilities(self):
        return frozenset({'demo.run'})

    def health(self):
        return {'status': 'ready'}

    def start(self, run, stage_view, emit):
        with self.lock:
            self.executions[stage_view.key] += 1
            self.views.append(stage_view)
            self.live += 1
            self.max_live = max(self.max_live, self.live)
        b = self.behaviour(stage_view.key, stage_view.attempt)
        for type_, data in b.get('events', ()):
            emit(type_, data)
        handle = Handle(run.id, stage_view.key, stage_view.attempt)
        handle.state = {'b': b, 'polls': 0, 'cancelled': False, 'approved': stage_view.approved, 'done': False}
        return handle

    def _done(self, handle):
        if not handle.state['done']:
            handle.state['done'] = True
            with self.lock:
                self.live -= 1

    def poll(self, handle):
        st, b = handle.state, handle.state['b']
        st['polls'] += 1
        if b.get('block') and b['block'] not in st['approved']:
            return {'state': 'blocked', 'exit_code': None, 'capability': b['block']}
        if st['cancelled'] and not b.get('stubborn'):
            self._done(handle)
            return {'state': 'exited', 'exit_code': 143}
        if b.get('hang') or st['polls'] <= b.get('ticks', 0):
            return {'state': 'running', 'exit_code': None}
        self._done(handle)
        return {'state': 'exited', 'exit_code': b.get('exit', 0)}

    def cancel(self, handle, grace_seconds):
        self.cancels.append((handle.stage_key, grace_seconds))
        handle.state['cancelled'] = True


class Harness:
    """Store with a fake epoch clock, a claimed run and a RunDriver on a fake monotonic clock."""

    def __init__(self, test, stages, *, script=None, provider='none', owner=OWNER, **spec_kw):
        self.test = test
        self.epoch = [1000.0]
        self.clock = Clock()
        self.store = support.store(test, clock=lambda: self.epoch[0])
        self.executor = FakeExecutor(script)
        self.owner = owner
        self.provider = provider
        spec = schema.demo_spec(stages=stages)
        spec.update(spec_kw)
        spec = schema.validate_spec(spec, provider=provider)
        version_id = self.store.ensure_inline_version('orch', spec)
        snapshot = schema.apply_overrides(spec, {}, provider=provider)
        self.run_id, _ = self.store.submit(version_id, snapshot, {}, idempotency_key=schema.new_id('run'),
                                           request_sha256=schema.sha256_hex(schema.canonical_json(spec)))
        self.driver = self.claim()

    def claim(self, owner=None, executor=None, **kw):
        owner = owner or self.owner
        row = self.store.claim_next(owner, lease_seconds=30, max_running=4)
        assert row is not None and row['id'] == self.run_id
        return self.driver_for(row, owner, executor, **kw)

    def driver_for(self, row, owner, executor=None, **kw):
        writer = Runner(self.store, {}, owner=owner)._write_artifact
        return RunDriver(self.store, row, executor or self.executor, owner=owner, write_artifact=writer,
                         clock=self.clock, lease_seconds=30, heartbeat_seconds=5, provider=self.provider, **kw)

    def advance(self, dt):
        self.clock.t += dt
        self.epoch[0] += dt

    def run(self, driver=None, *, dt=0.1, limit=5000):
        driver = driver or self.driver
        for _ in range(limit):
            out = driver.step()
            if out:
                return out
            self.advance(dt)
        raise AssertionError('driver did not finish')

    def steps(self, n, driver=None, dt=0.1):
        driver = driver or self.driver
        for _ in range(n):
            assert driver.step() is None
            self.advance(dt)

    def stage_status(self):
        """Latest attempt status per stage key."""
        return {s['key']: s['status'] for s in self.store.stages(self.run_id)}

    def attempts(self, key):
        return [(s['attempt'], s['status'], s['reason']) for s in self.store.stages(self.run_id) if s['key'] == key]

    def run_row(self):
        return self.store.run_row(self.run_id)

    def audit_actions(self):
        return [(e['action'], e['stage_key']) for e in self.store.audit(self.run_id)]
