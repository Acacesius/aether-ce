"""Workflow orchestration for one run (AHS-05). CE only: imported by the CE runner, never in live mode.

``RunDriver`` is a cooperative, single-threaded state machine. ``step()`` polls running executors, applies budget,
cancel and failure policy, and starts every stage whose ``needs`` all succeeded (bounded by the per-run, per-fan-out
and global caps). All state lives in the store (stage attempts, expansions, audit), so a driver built on a re-claimed
run continues from its checkpoint and never re-executes a completed stage. Time comes from an injected clock, which
makes every path testable without sleeping.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import threading
import time

from aether.studio import schema
from aether.studio.executors.base import ExecutorUnavailable, RunView, StageView
from aether.studio.orchestration_store import idempotency_key
from aether.studio.schema import SchemaError

HEARTBEAT_EVENT_SECONDS = 30
LOG_TEXT_MAX = 4000
VERDICTS = ('pass', 'fail')
EXECUTOR_EVENTS = ('stage.log', 'limit.exceeded', 'executor.error', 'adapter.session', 'adapter.event')
STOP_REASONS = {'cancel': 'cancelled_by_owner', 'budget': 'budget_exceeded', 'stop': 'stopped_on_failure'}


class Slots:
    """Global cap on concurrently running stages across all runs of one Runner."""

    def __init__(self, size: int):
        assert size >= 1
        self.size = size
        self._used = 0
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        with self._lock:
            if self._used >= self.size:
                return False
            self._used += 1
            return True

    def release(self):
        with self._lock:
            assert self._used > 0, 'slot released twice'
            self._used -= 1


@dataclass
class Live:
    spec: dict
    attempt: int
    handle: object
    deadline: float
    stop_reason: str | None = None
    stop_until: float = 0.0
    plan: object = None
    verdict: dict | None = None
    events: list = field(default_factory=list)


class RunDriver:
    def __init__(self, store, row, executor, *, owner, write_artifact, clock=time.monotonic, slots=None,
                 lease_seconds=30.0, heartbeat_seconds=5.0, provider='none'):
        self.store, self.executor, self.owner = store, executor, owner
        self.run_id = row['id']
        self.snapshot = json.loads(row['snapshot_json'])
        self.view = RunView(self.run_id, bool(row['demo']), self.snapshot)
        self.write_artifact, self.clock, self.slots = write_artifact, clock, slots
        self.lease_seconds, self.heartbeat_seconds, self.provider = lease_seconds, heartbeat_seconds, provider
        limits = self.snapshot['limits']
        self.limits = limits
        self.grace = self.snapshot['cancellation']['grace_seconds']
        self.stop_on_failure = self.snapshot.get('on_failure') == 'stop'
        self.t0 = clock()
        self.wall_base = row['wall_used']
        self.beat_next = self.t0 + heartbeat_seconds
        self.beat_event = self.t0
        self.live: dict[str, Live] = {}
        self.halt: tuple[str, str | None] | None = None
        self.outcome = None
        self.not_before: dict[str, float] = {}
        self.verdicts: dict[str, dict] = {}
        self.usage_dirty = False
        self.budget_hit = False
        self._load()

    # ------------------------------------------------------------ state
    def _load(self):
        store = self.store
        self.specs = {s['key']: s for s in store.graph(self.run_id)}
        self.expanded = {item['planner_key'] for item in store.expansions(self.run_id)}
        self.order = schema.topo_order(list(self.specs.values()))
        self.status = {}
        self.attempt = {}
        for row in store.stages(self.run_id):  # rowid order: the last row per key is its latest attempt
            self.status[row['key']] = row['status']
            self.attempt[row['key']] = row['attempt']
        for key, status in self.status.items():
            assert status != 'running', f'stage {key} still running at claim; recovery must run first'
            if status in ('blocked', 'interrupted'):
                assert store.requeue_stage(self.run_id, self.owner, key, status), 'lease lost while resuming'
                self.status[key] = 'queued'
                self.attempt[key] += 1
        self.produced = {a['name'] for a in store.get_run(self.run_id)['artifacts']}
        self.artifact_bytes = [sum(a['size'] for a in store.get_run(self.run_id)['artifacts'])]
        self.last_exit = None

    # ------------------------------------------------------------ the loop
    def step(self):
        """One pass. Returns None while running, else 'done' (run ended or blocked) or 'lost' (lease gone)."""
        if self.outcome:
            return self.outcome
        now = self.clock()
        state = self.store.owns(self.run_id, self.owner)
        if state is None or not self._beat(now):
            return self._lose()
        if state['cancel_requested'] and self.halt is None:
            self._halt('cancel')
        self._poll(now)
        if self.outcome:
            return self.outcome
        if self.halt is None:
            self._budgets(now)
            self._schedule(now)
        return self._maybe_finish()

    def shutdown(self):
        """Runner stopping: abandon executors but leave rows/lease as they are; recovery interrupts them later."""
        for key, live in list(self.live.items()):
            self._cancel_handle(live, 0)
            self._release(key)

    def _lose(self):
        self.shutdown()
        self.outcome = 'lost'
        return self.outcome

    def _beat(self, now):
        if now < self.beat_next:
            return True
        self.beat_next = now + self.heartbeat_seconds
        emit = now - self.beat_event >= HEARTBEAT_EVENT_SECONDS
        if emit:
            self.beat_event = now
        return self.store.heartbeat(self.run_id, self.owner, lease_seconds=self.lease_seconds, emit_event=emit,
                                    wall_used=self._wall(now))

    def _wall(self, now):
        return self.wall_base + (now - self.t0)

    # ------------------------------------------------------------ halting and budgets
    def _halt(self, kind, reason=None):
        assert self.halt is None
        self.halt = (kind, reason)
        if kind in ('approval', 'limit'):
            return  # other running stages finish on their own; nothing new starts
        self.store.audit_event(self.run_id, self.owner, 'runner', f'run.{kind}', None,
                               {'reason': reason or STOP_REASONS[kind], 'running': sorted(self.live)})
        for live in self.live.values():
            self._begin_stop(live, STOP_REASONS[kind])

    def _begin_stop(self, live, reason):
        if live.stop_reason is None:
            live.stop_reason = reason
            live.stop_until = self.clock() + self.grace
            self._cancel_handle(live, self.grace)

    def _cancel_handle(self, live, grace):
        try:
            self.executor.cancel(live.handle, grace)
        except Exception:
            pass  # a failing cancel is bounded by the grace deadline; the stage is then ended regardless

    def _budgets(self, now):
        limit = self._exceeded(now)
        if limit:
            name, value, cap = limit
            self.store.append_event(self.run_id, 'limit.exceeded', {'limit': name, 'value': value}, owner=self.owner)
            self.store.audit_event(self.run_id, self.owner, 'runner', 'budget.exceeded', None,
                                   {'limit': name, 'value': value, 'cap': cap})
            self._halt('budget')

    def _exceeded(self, now):
        wall = self._wall(now)
        if wall > self.limits['wall_seconds']:
            return 'wall_seconds', round(wall, 3), self.limits['wall_seconds']
        if self.usage_dirty:
            self.usage_dirty = False
            used = self.store.usage(self.run_id)
            for name, cap in (('tokens', self.limits['tokens']), ('cost_usd_cents', self.limits['cost_usd_cents'])):
                if cap and used[name] > cap:
                    return name, used[name], cap
        return None

    # ------------------------------------------------------------ polling running stages
    def _poll(self, now):
        for key, live in list(self.live.items()):
            if self.outcome:
                return
            try:
                result = self.executor.poll(live.handle)
            except Exception as error:
                self._emit(key, live, 'executor.error', {'code': 'poll_failed', 'message': type(error).__name__})
                self._cancel_handle(live, 0)
                self._end_attempt(key, live, 'failed', 'executor_error', None)
                continue
            state = result.get('state')
            if state == 'exited':
                self._exited(key, live, result)
            elif state == 'blocked':
                capability = str(result.get('capability', ''))[:64]
                self.store.create_approval(self.run_id, self.owner, key, capability)
                self._end_attempt(key, live, 'blocked', 'approval_pending', None)
                if self.halt is None:
                    self._halt('approval')
            elif state == 'limit_stop':
                # CLI executor: the slot ran out of quota. Not a failure: the run blocks until the owner resumes
                # it (or the queue driver re-queues it on another slot). No new DB reason: owner_action_required.
                detail = {'outcome': 'limit_stop', 'slot': str(result.get('slot', ''))[:40],
                          'blocked_until': str(result.get('blocked_until', ''))[:30]}
                self._release(key)
                if not self.store.end_stage(self.run_id, self.owner, key, live.attempt, 'blocked',
                                            reason='owner_action_required', exit_code=None, detail=detail):
                    return self._lose()
                self.status[key] = 'blocked'
                if self.halt is None:
                    self._halt('limit')
            elif live.stop_reason is None and now > live.deadline:
                self._begin_stop(live, 'timeout')
            elif live.stop_reason is not None and now > live.stop_until:
                self._cancel_handle(live, 0)
                self._end_stopped(key, live)
            if self.halt is None and self.usage_dirty:
                self._budgets(now)

    def _exited(self, key, live, result):
        code = result.get('exit_code')
        if code == 0:
            self._succeeded(key, live)
        elif live.stop_reason:  # we asked it to stop: our reason wins over whatever the executor reports
            self._end_stopped(key, live, code)
        elif code is None:  # unknown outcome is failure, never success
            reason = result.get('reason', 'executor_error')
            assert reason in schema.REASONS, f'executor returned unknown reason {reason!r}'
            self._end_attempt(key, live, 'failed', reason, None)
        else:
            self._end_attempt(key, live, 'failed', 'exit_nonzero', code)

    def _end_stopped(self, key, live, code=None):
        if live.stop_reason == 'timeout':
            self._end_attempt(key, live, 'failed', 'timeout', code)
        else:
            self._end_attempt(key, live, 'cancelled', live.stop_reason, code)

    # ------------------------------------------------------------ attempt outcomes
    def _succeeded(self, key, live):
        spec = live.spec
        role = schema.stage_role(spec)
        expansion = None
        if role == 'reviewer':
            verdict = live.verdict
            if verdict is None:
                self._emit(key, live, 'executor.error', {'code': 'verdict_missing', 'message': key})
                return self._end_attempt(key, live, 'failed', 'executor_error', 0)
            self._record_verdict(key, live, verdict)
            if verdict['verdict'] == 'fail':
                return self._end_attempt(key, live, 'failed', 'review_rejected', 0)
        elif role == 'planner' and live.plan is not None:
            try:
                assert key not in self.expanded, 'planner already expanded'
                expansion = schema.validate_expansion(self.snapshot, spec, list(self.specs.values()), live.plan,
                                                      provider=self.provider)
            except (SchemaError, AssertionError) as error:
                detail = error.as_dict() if isinstance(error, SchemaError) else {'detail': str(error)}
                self.store.audit_event(self.run_id, self.owner, f'planner:{key}', 'plan.rejected', key, detail)
                self._emit(key, live, 'executor.error', {'code': 'expansion_rejected', 'message': detail.get('detail', '')})
                return self._end_attempt(key, live, 'failed', 'expansion_rejected', 0)
        self._end_attempt(key, live, 'succeeded', None, 0, expansion=expansion)

    def _record_verdict(self, key, live, verdict):
        text = schema.canonical_json({'stage': key, 'attempt': live.attempt, **verdict})
        self.write_artifact(self.view, StageView(key, live.attempt, '', 1, ('verdict',)),
                            {'name': 'verdict', 'media_type': 'application/json', 'content': text},
                            self.produced, self.artifact_bytes, system=True)
        self.store.append_event(self.run_id, 'review.verdict', {'verdict': verdict['verdict'],
                                                                'reason': verdict['reason']}, stage_key=key, owner=self.owner)
        self.store.audit_event(self.run_id, self.owner, f'reviewer:{key}', 'review.verdict', key,
                               {'verdict': verdict['verdict'], 'reason': verdict['reason'], 'attempt': live.attempt})
        self.verdicts[key] = verdict

    def _end_attempt(self, key, live, status, reason, exit_code, *, expansion=None):
        self._release(key)
        retry = (status == 'failed' and reason == 'exit_nonzero' and self.halt is None
                 and live.attempt < schema.max_attempts(self.snapshot, live.spec))
        if not self.store.end_stage(self.run_id, self.owner, key, live.attempt, status, reason=reason,
                                    exit_code=exit_code, retry=retry, expansion=expansion):
            return self._lose()
        if status == 'succeeded':
            self.last_exit = 0
        self.status[key] = 'queued' if retry else status
        if retry:
            self.attempt[key] = live.attempt + 1
            self.not_before[key] = self.clock() + self.snapshot['retry']['backoff_seconds']
        if expansion:
            self.expanded.add(key)
            for stage in expansion:
                self.specs[stage['key']] = stage
                self.status[stage['key']] = 'queued'
                self.attempt[stage['key']] = 1
            self.order = schema.topo_order(list(self.specs.values()))
        if status == 'failed' and not retry and not live.spec.get('optional', False) \
                and self.stop_on_failure and self.halt is None:
            self._halt('stop', reason)

    def _release(self, key):
        if self.live.pop(key, None) is not None and self.slots is not None:
            self.slots.release()

    # ------------------------------------------------------------ scheduling
    def _schedule(self, now):
        skips = self._propagate_skips()
        if skips:
            if not self.store.settle_pending(self.run_id, self.owner, skips):
                return self._lose()
        cap = self.limits['max_workers']
        for key in self.order:
            if self.halt or self.outcome or len(self.live) >= cap:
                return
            if self.status[key] != 'queued' or key in self.live or not self._ready(key, now):
                continue
            if self.slots is not None and not self.slots.try_acquire():
                return
            self._start(key, now)

    def _propagate_skips(self):
        skips = []
        for key in self.order:  # topological: a skip cascades through the whole branch in one pass
            if self.status[key] == 'queued' and key not in self.live and any(
                    self.status[n] in ('failed', 'skipped', 'cancelled') for n in self.specs[key]['needs']):
                self.status[key] = 'skipped'
                skips.append((key, 'skipped', 'upstream_failed'))
        return skips

    def _ready(self, key, now):
        spec = self.specs[key]
        if now < self.not_before.get(key, 0) or any(self.status[n] != 'succeeded' for n in spec['needs']):
            return False
        for parent in spec['needs']:  # per-fan-out lane cap declared on the stage being fanned out from
            lanes = self.specs[parent].get('max_workers')
            if lanes and sum(parent in self.specs[k]['needs'] for k in self.live) >= lanes:
                return False
        return True

    def _start(self, key, now):
        spec, attempt = self.specs[key], self.attempt[key]
        if not self.store.claim_stage(self.run_id, self.owner, key, attempt):
            if self.slots is not None:
                self.slots.release()
            return self._lose()  # lease gone, or another claimant owns this attempt: never execute it twice
        live = Live(spec, attempt, None, now + spec['timeout_seconds'])
        self.live[key] = live
        self.status[key] = 'running'
        stage = StageView(key, attempt, spec['executor_step'], spec['timeout_seconds'], tuple(spec['outputs']),
                          idempotency_key=idempotency_key(self.run_id, key, attempt), role=schema.stage_role(spec),
                          upstream=self._upstream(spec), approved=self.store.granted_capabilities(self.run_id, key))
        try:
            live.handle = self.executor.start(self.view, stage, lambda type_, data: self._emit(key, live, type_, data, stage))
        except ExecutorUnavailable:
            return self._end_attempt(key, live, 'failed', 'adapter_unavailable', None)
        except Exception as error:
            self._emit(key, live, 'executor.error', {'code': 'start_failed', 'message': type(error).__name__})
            return self._end_attempt(key, live, 'failed', 'executor_error', None)
        self.store.audit_event(self.run_id, self.owner, 'runner', 'stage.started', key, {'attempt': attempt})

    def _upstream(self, spec):
        if schema.stage_role(spec) != 'reviewer':
            return ()
        artifacts = self.store.stage_artifacts(self.run_id, spec['needs'])
        return tuple({'stage': n, 'status': self.status[n], 'verdict': self.verdicts.get(n),
                      'artifacts': [a for a in artifacts if a['stage_key'] == n]} for n in spec['needs'])

    # ------------------------------------------------------------ executor output
    def _emit(self, key, live, type_, data, stage=None):
        if type_ == 'artifact.write':
            return self.write_artifact(self.view, stage or StageView(key, live.attempt, '', 1, tuple(live.spec['outputs'])),
                                       data, self.produced, self.artifact_bytes)
        role = schema.stage_role(live.spec)
        if type_ == 'stage.usage':
            return self._usage(key, live, data)
        if type_ == 'stage.plan' and role == 'planner' and live.plan is None:
            live.plan = data.get('stages') if isinstance(data, dict) else None
            data = {'proposals': len(live.plan) if isinstance(live.plan, list) else None}
        elif type_ == 'stage.verdict' and role == 'reviewer' and live.verdict is None:
            live.verdict = _verdict(data)
            return None if live.verdict else self._emit(key, live, 'executor.error',
                                                        {'code': 'verdict_invalid', 'message': key})
        elif type_ == 'stage.log':
            data = dict(data)
            data['text'] = str(data.get('text', ''))[:LOG_TEXT_MAX]
            data.pop('demo', None)
        elif type_ not in EXECUTOR_EVENTS:
            type_, data = 'executor.error', {'code': 'unknown_event', 'message': str(type_)[:300]}
        return self.store.append_event(self.run_id, type_, data, stage_key=key, owner=self.owner)

    def _usage(self, key, live, data):
        tokens, cost = data.get('tokens', 0), data.get('cost_usd_cents', 0)
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in (tokens, cost)):
            return self._emit(key, live, 'executor.error', {'code': 'usage_invalid', 'message': key})
        self.store.add_usage(self.run_id, self.owner, key, live.attempt, tokens, cost)
        self.usage_dirty = True

    # ------------------------------------------------------------ finishing
    def _maybe_finish(self):
        if self.outcome or self.live:
            return None
        queued = [k for k in self.order if self.status[k] == 'queued']
        if self.halt is None and queued:
            return None
        kind, reason = self.halt or (None, None)
        if kind == 'approval':
            return self._end('blocked', 'approval_pending', None)
        if kind == 'limit':
            return self._end('blocked', 'owner_action_required', None)
        if kind in ('cancel', 'budget', 'stop'):
            if not self._settle(queued, 'cancelled' if kind == 'cancel' else 'skipped', STOP_REASONS[kind]):
                return self.outcome
        if kind == 'cancel':
            return self._end('cancelled', 'cancelled_by_owner', None)
        if kind in ('budget', 'stop'):
            return self._end('failed', 'budget_exceeded' if kind == 'budget' else reason, self._failed_exit())
        return self._conclude()

    def _settle(self, keys, to, reason):
        """Move still-queued stages to their final state; False (and outcome 'lost') if the lease is gone."""
        if keys and not self.store.settle_pending(self.run_id, self.owner, [(k, to, reason) for k in keys]):
            self._lose()
            return False
        for key in keys:
            self.status[key] = to
        return True

    def _failed_exit(self):
        return next((self._exit_of(k) for k in self.order if self.status[k] == 'failed' and self._required(k)), None)

    def _exit_of(self, key):
        return next((r['exit_code'] for r in reversed(self.store.stages(self.run_id)) if r['key'] == key), None)

    def _required(self, key):
        return not self.specs[key].get('optional', False)

    def _conclude(self):
        """No halt, nothing running or queued: the run is succeeded only if every required stage succeeded."""
        required = [k for k in self.order if self._required(k)]
        failed = next((k for k in required if self.status[k] == 'failed'), None)
        if failed:
            rows = [r for r in self.store.stages(self.run_id) if r['key'] == failed]
            return self._end('failed', rows[-1]['reason'], rows[-1]['exit_code'])
        if any(self.status[k] != 'succeeded' for k in required):  # skipped/cancelled required stage: never success
            return self._end('failed', 'dependency_failed', None)
        missing = [n for n in self.snapshot['evidence']['outputs'] if n not in self.produced]
        if missing:
            self.store.append_event(self.run_id, 'executor.error', {'code': 'evidence_missing',
                                                                    'message': ','.join(missing)[:300]}, owner=self.owner)
            return self._end('failed', 'executor_error', self.last_exit)
        return self._end('succeeded', None, self.last_exit)

    def _end(self, status, reason, exit_code):
        if not self.store.finish(self.run_id, self.owner, status, reason, exit_code):
            return self._lose()
        self.outcome = 'done'
        return self.outcome


def _verdict(data):
    if not isinstance(data, dict) or data.get('verdict') not in VERDICTS:
        return None
    return {'verdict': data['verdict'], 'reason': str(data.get('reason', ''))[:300]}
