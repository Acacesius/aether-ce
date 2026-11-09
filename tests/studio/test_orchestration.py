"""AHS-05 orchestration: deterministic (fake clock, scripted executor, no sleeps, no threads except the claim race)."""
from __future__ import annotations

import json
import sqlite3
import threading
import unittest

from aether.studio import schema
from aether.studio.store import StoreError
from tests.studio.orch_support import FakeExecutor, Harness, stage


def diamond(**extra):
    return [stage('a', **extra), stage('b', ['a'], **extra), stage('c', ['a'], **extra), stage('d', ['b', 'c'], **extra)]


class DagTests(unittest.TestCase):
    def test_diamond_succeeds_in_dependency_order_with_parallel_middle(self):
        h = Harness(self, diamond(), script={k: {'ticks': 2} for k in 'abcd'}, limits={'wall_seconds': 600, 'max_workers': 2})
        self.assertEqual(h.run(), 'done')
        run = h.run_row()
        self.assertEqual((run['status'], run['reason']), ('succeeded', None))
        self.assertEqual(h.stage_status(), {k: 'succeeded' for k in 'abcd'})
        self.assertEqual(dict(h.executor.executions), {k: 1 for k in 'abcd'})
        self.assertEqual(h.executor.max_live, 2, 'b and c run in parallel, never more than max_workers')
        order = [v.key for v in h.executor.views]
        self.assertEqual((order[0], order[-1]), ('a', 'd'))

    def test_run_worker_cap_is_never_exceeded(self):
        stages = [stage(k) for k in 'abcdef']
        h = Harness(self, stages, script={k: {'ticks': 3} for k in 'abcdef'}, limits={'wall_seconds': 600, 'max_workers': 3})
        h.run()
        self.assertEqual(h.executor.max_live, 3)
        self.assertEqual(h.run_row()['status'], 'succeeded')

    def test_failure_skips_dependants_and_independent_branch_continues(self):
        stages = [stage('a', step='mock:fail:3'), stage('b', ['a']), stage('c'), stage('d', ['b']), stage('e', ['c'])]
        h = Harness(self, stages, script={'a': {'exit': 3}})
        h.run()
        self.assertEqual(h.stage_status(), {'a': 'failed', 'b': 'skipped', 'c': 'succeeded', 'd': 'skipped', 'e': 'succeeded'})
        self.assertEqual([r for r in h.attempts('b')], [(1, 'skipped', 'upstream_failed')])
        run = h.run_row()
        self.assertEqual((run['status'], run['reason'], run['exit_code']), ('failed', 'exit_nonzero', 3))
        self.assertNotIn('b', h.executor.executions)
        self.assertNotIn('d', h.executor.executions)

    def test_on_failure_stop_cancels_running_and_skips_the_rest(self):
        stages = [stage('x'), stage('y'), stage('z', ['x'])]
        h = Harness(self, stages, script={'x': {'hang': True}, 'y': {'ticks': 1, 'exit': 1}}, on_failure='stop',
                    limits={'wall_seconds': 600, 'max_workers': 2}, cancellation={'grace_seconds': 7})
        h.run()
        self.assertEqual(h.stage_status(), {'x': 'cancelled', 'y': 'failed', 'z': 'skipped'})
        self.assertEqual(h.attempts('x')[0][2], 'stopped_on_failure')
        self.assertEqual(h.attempts('z')[0][2], 'stopped_on_failure')
        self.assertEqual(h.executor.cancels, [('x', 7)], 'running stages get cancel-with-grace')
        self.assertEqual((h.run_row()['status'], h.run_row()['reason']), ('failed', 'exit_nonzero'))

    def test_default_policy_is_continue_independent(self):
        h = Harness(self, [stage('x'), stage('y')], script={'x': {'exit': 1}})
        h.run()
        self.assertEqual(h.stage_status(), {'x': 'failed', 'y': 'succeeded'})

    def test_optional_failure_does_not_fail_the_run_but_required_dependants_skip(self):
        h = Harness(self, [stage('o', optional=True), stage('r')], script={'o': {'exit': 1}})
        h.run()
        self.assertEqual((h.run_row()['status'], h.stage_status()), ('succeeded', {'o': 'failed', 'r': 'succeeded'}))
        h2 = Harness(self, [stage('o', optional=True), stage('r', ['o'])], script={'o': {'exit': 1}})
        h2.run()
        self.assertEqual(h2.stage_status(), {'o': 'failed', 'r': 'skipped'})
        self.assertEqual((h2.run_row()['status'], h2.run_row()['reason']), ('failed', 'dependency_failed'),
                         'a skipped required stage is never reported as success')

    def test_global_slot_cap_is_shared_between_runs(self):
        from aether.studio.orchestrator import Slots
        slots = Slots(1)
        h = Harness(self, [stage('a'), stage('b')], script={'a': {'ticks': 2}, 'b': {'ticks': 2}},
                    limits={'wall_seconds': 600, 'max_workers': 4})
        h.driver.slots = slots
        h.run()
        self.assertEqual(h.executor.max_live, 1)
        self.assertEqual(h.run_row()['status'], 'succeeded')

    def test_fan_out_lane_cap(self):
        stages = [stage('p', max_workers=2)] + [stage(f'w{i}', ['p']) for i in range(4)]
        h = Harness(self, stages, script={f'w{i}': {'ticks': 3} for i in range(4)}, limits={'wall_seconds': 600, 'max_workers': 4})
        h.run()
        self.assertEqual(h.executor.max_live, 2)
        self.assertEqual(h.run_row()['status'], 'succeeded')


class TimeoutRetryTests(unittest.TestCase):
    def test_stage_timeout_cancels_with_grace_then_fails(self):
        h = Harness(self, [stage('a', timeout_seconds=5)], script={'a': {'hang': True}}, cancellation={'grace_seconds': 3})
        h.run()
        self.assertEqual(h.attempts('a'), [(1, 'failed', 'timeout')])
        self.assertEqual(h.executor.cancels, [('a', 3)])
        self.assertEqual((h.run_row()['status'], h.run_row()['reason']), ('failed', 'timeout'))

    def test_executor_ignoring_cancel_is_ended_after_grace(self):
        h = Harness(self, [stage('a', timeout_seconds=5)], script={'a': {'hang': True, 'stubborn': True}},
                    cancellation={'grace_seconds': 2})
        before = h.clock.t
        h.run()
        self.assertEqual(h.attempts('a'), [(1, 'failed', 'timeout')])
        self.assertEqual(h.executor.cancels, [('a', 2), ('a', 0)], 'graceful cancel, then a hard stop')
        self.assertGreaterEqual(h.clock.t - before, 7)

    def test_retry_then_success_creates_new_attempt_records(self):
        h = Harness(self, [stage('a', retry_max=2)], script={'a': [{'exit': 1}, {'exit': 1}, {'exit': 0}]})
        h.run()
        self.assertEqual(h.attempts('a'), [(1, 'failed', 'exit_nonzero'), (2, 'failed', 'exit_nonzero'), (3, 'succeeded', None)])
        self.assertEqual(h.executor.executions['a'], 3)
        keys = {s['idem_key'] for s in h.store.stages(h.run_id)}
        self.assertEqual(len(keys), 3, 'one idempotency key per (run, stage, attempt)')
        self.assertEqual(h.run_row()['status'], 'succeeded')
        self.assertIn(('stage.retried', 'a'), h.audit_actions())

    def test_retry_exhaustion_fails_the_run(self):
        h = Harness(self, [stage('a', retry_max=1), stage('b', ['a'])], script={'a': {'exit': 9}})
        h.run()
        self.assertEqual(h.executor.executions['a'], 2)
        self.assertEqual(h.stage_status(), {'a': 'failed', 'b': 'skipped'})
        self.assertEqual((h.run_row()['status'], h.run_row()['exit_code']), ('failed', 9))

    def test_blueprint_retry_is_the_attempt_floor_and_backoff_is_honoured(self):
        h = Harness(self, [stage('a')], script={'a': [{'exit': 1}, {'exit': 0}]}, retry={'max_attempts': 2, 'backoff_seconds': 30})
        h.steps(100)  # 10 fake seconds: attempt 1 failed, attempt 2 must still be waiting out its backoff
        self.assertEqual(h.executor.executions['a'], 1)
        self.assertEqual(h.attempts('a')[-1][1], 'queued')
        h.run()
        self.assertEqual(h.executor.executions['a'], 2)
        self.assertEqual(h.run_row()['status'], 'succeeded')

    def test_non_retriable_reasons_are_not_retried(self):
        h = Harness(self, [stage('a', retry_max=3, timeout_seconds=2)], script={'a': {'hang': True}})
        h.run()
        self.assertEqual(h.executor.executions['a'], 1)

    def test_attempts_are_never_concurrent(self):
        h = Harness(self, [stage('a', retry_max=3)], script={'a': {'ticks': 2, 'exit': 1}})
        h.run()
        self.assertEqual(h.executor.max_live, 1)
        self.assertEqual(h.executor.executions['a'], 4)


class BudgetTests(unittest.TestCase):
    def usage(self, tokens, cost=0):
        return {'ticks': 3, 'events': [('stage.usage', {'tokens': tokens, 'cost_usd_cents': cost})]}

    def test_token_budget_is_summed_across_stages_and_stops_scheduling(self):
        stages = [stage('a'), stage('b'), stage('c', ['a', 'b'])]
        h = Harness(self, stages, provider='local', script={'a': self.usage(60), 'b': self.usage(60)},
                    limits={'wall_seconds': 600, 'tokens': 100, 'max_workers': 2}, cancellation={'grace_seconds': 4})
        h.run()
        run = h.run_row()
        self.assertEqual((run['status'], run['reason']), ('failed', 'budget_exceeded'))
        self.assertEqual(h.stage_status(), {'a': 'cancelled', 'b': 'cancelled', 'c': 'skipped'})
        self.assertEqual(sorted(h.executor.cancels), [('a', 4), ('b', 4)])
        self.assertNotIn('c', h.executor.executions)
        self.assertIn(('budget.exceeded', None), h.audit_actions())

    def test_budget_sums_across_retries(self):
        h = Harness(self, [stage('a', retry_max=3)], provider='local',
                    script={'a': {'exit': 1, 'events': [('stage.usage', {'tokens': 40, 'cost_usd_cents': 0})]}},
                    limits={'wall_seconds': 600, 'tokens': 100})
        h.run()
        self.assertEqual(h.executor.executions['a'], 3, 'the third attempt pushes the run past 100 tokens')
        self.assertEqual(h.run_row()['reason'], 'budget_exceeded')

    def test_cost_budget(self):
        h = Harness(self, [stage('a')], provider='local', script={'a': self.usage(0, 51)},
                    limits={'wall_seconds': 600, 'cost_usd_cents': 50})
        h.run()
        self.assertEqual(h.run_row()['reason'], 'budget_exceeded')
        self.assertEqual(h.store.usage(h.run_id), {'tokens': 0, 'cost_usd_cents': 51})

    def test_within_budget_succeeds(self):
        h = Harness(self, [stage('a')], provider='local', script={'a': self.usage(100)}, limits={'wall_seconds': 600, 'tokens': 100})
        h.run()
        self.assertEqual(h.run_row()['status'], 'succeeded')

    def test_wall_budget_counts_all_stages_and_retries(self):
        stages = [stage('a', retry_max=1), stage('b', ['a'])]
        h = Harness(self, stages, script={'a': [{'ticks': 40, 'exit': 1}, {'ticks': 40}], 'b': {'ticks': 40}},
                    limits={'wall_seconds': 9})  # each stage is ~4 s; the sum, not any single stage, exceeds 9 s
        h.run()
        run = h.run_row()
        self.assertEqual((run['status'], run['reason']), ('failed', 'budget_exceeded'))
        self.assertIn('limit.exceeded', [e['type'] for e in h.store.events(h.run_id, 0, 500)[0]])

    def test_wall_budget_survives_a_restart(self):
        h = Harness(self, [stage('a', restartable=True)], script={'a': {'hang': True}}, limits={'wall_seconds': 100},
                    cancellation={'grace_seconds': 1})
        h.advance(60)
        h.steps(60)  # heartbeats checkpoint ~66 s of wall time
        h.driver.shutdown()
        h.advance(100)  # crash: lease expires
        self.assertEqual(h.store.recover_expired_leases(), {h.run_id: 'queued'})
        self.assertGreater(h.run_row()['wall_used'], 60)
        driver = h.claim()
        h.executor.script['a'] = {'hang': True}
        h.run(driver)
        self.assertEqual(h.run_row()['reason'], 'budget_exceeded', 'time already used counts after the restart')


class CancelTests(unittest.TestCase):
    def test_cancel_mid_run(self):
        stages = [stage('a'), stage('b', ['a']), stage('c')]
        h = Harness(self, stages, script={'a': {'hang': True}, 'c': {'hang': True}}, cancellation={'grace_seconds': 5},
                    limits={'wall_seconds': 600, 'max_workers': 2})
        h.steps(5)
        self.assertEqual(h.store.request_cancel(h.run_id), 'cancel_requested')
        h.run()
        self.assertEqual(h.stage_status(), {'a': 'cancelled', 'b': 'cancelled', 'c': 'cancelled'})
        self.assertEqual(sorted(h.executor.cancels), [('a', 5), ('c', 5)])
        self.assertNotIn('b', h.executor.executions, 'no new stage starts after cancel')
        run = h.run_row()
        self.assertEqual((run['status'], run['reason']), ('cancelled', 'cancelled_by_owner'))
        actions = [a for a, _ in h.audit_actions()]
        self.assertIn('run.cancel_requested', actions)
        self.assertEqual(actions[-1], 'run.finished')


class RecoveryTests(unittest.TestCase):
    def crash(self, h, driver=None):
        (driver or h.driver).shutdown()
        h.advance(100)  # lease (30 s) expires
        return h.store.recover_expired_leases()

    def test_restart_never_re_executes_completed_stages_non_restartable_blocks_for_owner(self):
        h = Harness(self, diamond(), script={'b': {'hang': True}, 'c': {'ticks': 1}}, limits={'wall_seconds': 600, 'max_workers': 2})
        h.steps(8)
        self.assertEqual(h.stage_status()['a'], 'succeeded')
        self.assertEqual(h.stage_status()['b'], 'running')
        self.assertEqual(self.crash(h), {h.run_id: 'blocked'})
        run = h.run_row()
        self.assertEqual((run['status'], run['reason']), ('blocked', 'owner_action_required'))
        self.assertEqual(h.attempts('b'), [(1, 'interrupted', 'lease_lost')])
        self.assertEqual(h.executor.executions['a'], 1)
        # nothing runs while blocked: no dispatcher can claim it
        self.assertIsNone(h.store.claim_next('owner-b', lease_seconds=30, max_running=4))
        # the owner accepts re-running the interrupted stage
        self.assertEqual(h.store.resume_run(h.run_id), 'queued')
        h.executor.script['b'] = {}
        driver = h.claim(owner='owner-b')
        h.run(driver)
        self.assertEqual(h.run_row()['status'], 'succeeded')
        self.assertEqual(h.executor.executions['a'], 1, 'a completed before the crash and is never re-executed')
        self.assertEqual(h.executor.executions['c'], 1)
        self.assertEqual(h.executor.executions['d'], 1)
        self.assertEqual(h.executor.executions['b'], 2, 'the interrupted attempt plus the owner-approved re-run')
        self.assertEqual(h.attempts('b'), [(1, 'interrupted', 'lease_lost'), (2, 'succeeded', None)])
        self.assertIn(('run.resumed', None), h.audit_actions())

    def test_restartable_stage_is_re_run_at_most_once(self):
        h = Harness(self, [stage('a'), stage('b', ['a'], restartable=True), stage('c', ['b'])],
                    script={'b': {'hang': True}})
        h.steps(6)
        self.assertEqual(self.crash(h), {h.run_id: 'queued'}, 'restartable: resumed automatically')
        driver = h.claim(owner='owner-b')
        h.steps(6, driver)
        self.assertEqual(h.stage_status()['b'], 'running')
        self.assertEqual(self.crash(h, driver), {h.run_id: 'blocked'}, 'second interruption: owner action required')
        self.assertEqual(h.executor.executions['a'], 1)
        self.assertEqual(h.executor.executions['b'], 2)
        self.assertEqual([a[1] for a in h.attempts('b')], ['interrupted', 'interrupted'])

    def test_no_running_stage_means_plain_resume(self):
        h = Harness(self, [stage('a'), stage('b', ['a'])], script={'a': {'ticks': 1}})
        h.driver.shutdown()
        h.advance(100)
        self.assertEqual(h.store.recover_expired_leases(), {h.run_id: 'queued'})
        h.run(h.claim(owner='owner-b'))
        self.assertEqual(h.run_row()['status'], 'succeeded')
        self.assertEqual(dict(h.executor.executions), {'a': 1, 'b': 1})

    def test_live_lease_is_not_recovered(self):
        h = Harness(self, [stage('a')], script={'a': {'hang': True}})
        h.steps(3)
        self.assertEqual(h.store.recover_expired_leases(), {})

    def test_cancel_requested_run_with_dead_runner_is_cancelled_on_recovery(self):
        h = Harness(self, [stage('a')], script={'a': {'hang': True}})
        h.steps(3)
        h.store.request_cancel(h.run_id)
        self.assertEqual(self.crash(h), {h.run_id: 'cancelled'})
        self.assertEqual(h.run_row()['status'], 'cancelled')


class ClaimRaceTests(unittest.TestCase):
    def test_duplicate_stage_claim_executes_once(self):
        h = Harness(self, [stage('a')])
        wins = []
        barrier = threading.Barrier(8)

        def claim():
            barrier.wait()
            wins.append(h.store.claim_stage(h.run_id, h.owner, 'a', 1))
        threads = [threading.Thread(target=claim) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(wins.count(True), 1)
        self.assertEqual(h.attempts('a'), [(1, 'running', None)])

    def test_second_driver_for_the_same_run_never_executes(self):
        h = Harness(self, [stage('a')], script={'a': {'ticks': 3}})
        row = h.run_row()
        rival = h.driver_for(row, 'owner-b')
        self.assertEqual(rival.step(), 'lost', 'it does not hold the run lease')
        self.assertEqual(h.executor.executions['a'], 0)
        h.run()
        self.assertEqual(h.executor.executions['a'], 1)

    def test_claiming_a_finished_attempt_again_is_refused(self):
        h = Harness(self, [stage('a')])
        h.run()
        self.assertEqual(h.run_row()['status'], 'succeeded')
        self.assertFalse(h.store.claim_stage(h.run_id, h.owner, 'a', 1))


def planner_script(proposals, **extra):
    return {'plan': dict({'events': [('stage.plan', {'stages': proposals})]}, **extra)}


def planner_stage(key='plan', **extra):
    return stage(key, role='planner', **extra)


class ExpansionTests(unittest.TestCase):
    def test_bounded_expansion_runs_the_added_stages_once(self):
        proposals = [stage('w1', ['plan']), stage('w2', ['plan']), stage('join', ['w1', 'w2'], role='reviewer')]
        script = planner_script(proposals)
        script['join'] = {'events': [('stage.verdict', {'verdict': 'pass', 'reason': 'ok'})]}
        h = Harness(self, [planner_stage()], script=script)
        h.run()
        self.assertEqual((h.run_row()['status'], h.stage_status()),
                         ('succeeded', {'plan': 'succeeded', 'w1': 'succeeded', 'w2': 'succeeded', 'join': 'succeeded'}))
        self.assertEqual(dict(h.executor.executions), {'plan': 1, 'w1': 1, 'w2': 1, 'join': 1})
        self.assertEqual([e['planner_key'] for e in h.store.expansions(h.run_id)], ['plan'])
        self.assertIn(('plan.expanded', 'plan'), h.audit_actions())
        self.assertEqual([s['key'] for s in h.store.get_run(h.run_id)['added_stages']], ['w1', 'w2', 'join'])

    def test_expansion_is_persisted_before_added_stages_run_and_survives_restart(self):
        script = planner_script([stage('w', ['plan'], restartable=True)])
        script['w'] = {'hang': True}
        h = Harness(self, [planner_stage()], script=script)
        h.steps(8)
        self.assertEqual(h.stage_status()['w'], 'running')
        h.driver.shutdown()
        h.advance(100)
        self.assertEqual(h.store.recover_expired_leases(), {h.run_id: 'queued'})
        h.executor.script['w'] = {}
        h.run(h.claim(owner='owner-b'))
        self.assertEqual(h.run_row()['status'], 'succeeded')
        self.assertEqual(h.executor.executions['plan'], 1, 'the planner is not re-run, so it cannot expand twice')

    def test_planner_cannot_expand_twice(self):
        h = Harness(self, [planner_stage()], script=planner_script([stage('w', ['plan'])]))
        h.run()
        with self.assertRaises(sqlite3.IntegrityError):
            with h.store.tx() as db:
                db.execute("INSERT INTO run_expansions(run_id, planner_key, stages_json, created_utc) VALUES (?,?,?,?)",
                           (h.run_id, 'plan', '[]', 'x'))

    def assert_rejected(self, proposals, base=None):
        h = Harness(self, base or [planner_stage()], script=planner_script(proposals))
        h.run()
        run = h.run_row()
        self.assertEqual((run['status'], run['reason']), ('failed', 'expansion_rejected'))
        self.assertEqual(h.store.expansions(h.run_id), [])
        self.assertEqual(h.attempts('plan'), [(1, 'failed', 'expansion_rejected')])
        self.assertIn(('plan.rejected', 'plan'), h.audit_actions())
        self.assertEqual(h.executor.executions['plan'], 1)
        return h

    def test_recursion_is_rejected(self):
        self.assert_rejected([stage('sub', ['plan'], role='planner')])

    def test_escalating_capability_is_rejected(self):
        h = self.assert_rejected([stage('w', ['plan'], capabilities=['demo.run', 'workspace.write'])])
        self.assertIn('capability_not_allowed', json.dumps(h.store.audit(h.run_id)))

    def test_unknown_or_forbidden_capability_is_rejected(self):
        self.assert_rejected([stage('w', ['plan'], capabilities=['shell.exec'])])

    def test_over_limit_is_rejected(self):
        base = [planner_stage()] + [stage(f's{i}') for i in range(schema.MAX_STAGES - 4)]
        h = self.assert_rejected([stage(f'w{i}', ['plan']) for i in range(8)], base)
        self.assertIn(f'exceed {schema.MAX_STAGES}', json.dumps(h.store.audit(h.run_id)))

    def test_too_many_proposals_is_rejected(self):
        self.assert_rejected([stage(f'w{i}', ['plan']) for i in range(schema.MAX_PROPOSALS + 1)])

    def test_depth_over_limit_is_rejected(self):
        chain = [stage(f'w{i}', ['plan' if i == 0 else f'w{i - 1}']) for i in range(schema.MAX_DEPTH)]
        self.assert_rejected(chain)

    def test_unanchored_duplicate_and_unknown_field_proposals_are_rejected(self):
        self.assert_rejected([stage('free')])
        self.assert_rejected([stage('plan', ['plan'])])
        self.assert_rejected([dict(stage('w', ['plan']), executor='other')])

    def test_non_planner_cannot_propose_stages(self):
        script = {'w': {'events': [('stage.plan', {'stages': [stage('x', ['w'])]})]}}
        h = Harness(self, [stage('w')], script=script)
        h.run()
        self.assertEqual(h.store.expansions(h.run_id), [])
        self.assertEqual(h.stage_status(), {'w': 'succeeded'})
        self.assertIn('unknown_event', json.dumps(h.store.events(h.run_id, 0, 100)[0]))

    def test_validate_expansion_unit(self):
        spec = schema.validate_spec(schema.demo_spec(stages=[planner_stage()]))
        planner = spec['stages'][0]
        added = schema.validate_expansion(spec, planner, spec['stages'], [stage('w', ['plan'])])
        self.assertEqual([s['key'] for s in added], ['w'])
        with self.assertRaises(schema.SchemaError):
            schema.validate_expansion(spec, spec['stages'][0] | {'role': 'worker'}, spec['stages'], [stage('w', ['plan'])])


class ReviewerTests(unittest.TestCase):
    def test_pass_verdict_is_recorded_as_event_artifact_and_audit(self):
        stages = [stage('work'), stage('review', ['work'], role='reviewer')]
        h = Harness(self, stages, script={'review': {'events': [('stage.verdict', {'verdict': 'pass', 'reason': 'looks right'})]}})
        h.run()
        self.assertEqual(h.run_row()['status'], 'succeeded')
        verdict_events = [e for e in h.store.events(h.run_id, 0, 500)[0] if e['type'] == 'review.verdict']
        self.assertEqual(verdict_events[0]['data'], {'verdict': 'pass', 'reason': 'looks right'})
        artifacts = h.store.get_run(h.run_id)['artifacts']
        self.assertEqual([(a['name'], a['stage_key']) for a in artifacts], [('verdict', 'review')])
        self.assertIn(('review.verdict', 'review'), h.audit_actions())

    def test_reviewer_sees_upstream_status_and_artifacts_and_verdict_passes_the_run(self):
        stages = [stage('work'), stage('review', ['work'], role='reviewer')]
        h = Harness(self, stages, script={'review': {'events': [('stage.verdict', {'verdict': 'pass', 'reason': 'ok'})]}})
        h.run()
        review = next(v for v in h.executor.views if v.key == 'review')
        self.assertEqual(review.role, 'reviewer')
        self.assertEqual([(u['stage'], u['status']) for u in review.upstream], [('work', 'succeeded')])
        self.assertEqual(h.run_row()['status'], 'succeeded')

    def test_fail_verdict_fails_the_run(self):
        stages = [stage('work'), stage('review', ['work'], role='reviewer')]
        h = Harness(self, stages, script={'review': {'events': [('stage.verdict', {'verdict': 'fail', 'reason': 'wrong'})]}})
        h.run()
        run = h.run_row()
        self.assertEqual((run['status'], run['reason']), ('failed', 'review_rejected'))
        self.assertEqual(h.attempts('review'), [(1, 'failed', 'review_rejected')])
        self.assertEqual(h.executor.executions['review'], 1, 'a rejected review is not retried')
        self.assertEqual([a['name'] for a in h.store.get_run(h.run_id)['artifacts']], ['verdict'])

    def test_missing_or_invalid_verdict_is_a_failure_never_a_pass(self):
        for verdict in (None, {'verdict': 'maybe'}):
            stages = [stage('work'), stage('review', ['work'], role='reviewer')]
            events = [] if verdict is None else [('stage.verdict', verdict)]
            h = Harness(self, stages, script={'review': {'events': events}})
            h.run()
            self.assertEqual((h.run_row()['status'], h.attempts('review')[0][1]), ('failed', 'failed'))

    def test_workers_cannot_emit_verdicts(self):
        h = Harness(self, [stage('w')], script={'w': {'events': [('stage.verdict', {'verdict': 'pass'})]}})
        h.run()
        self.assertEqual(h.store.get_run(h.run_id)['artifacts'], [])
        self.assertNotIn('review.verdict', [e['type'] for e in h.store.events(h.run_id, 0, 100)[0]])


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self, diamond(), script={'a': {'exit': 1}})
        self.h.run()

    def test_chain_verifies_and_covers_the_decisions(self):
        h = self.h
        report = h.store.verify_audit(h.run_id)
        self.assertTrue(report['ok'])
        entries = h.store.audit(h.run_id)
        self.assertEqual(report['entries'], len(entries))
        self.assertEqual(report['head'], entries[-1]['hash'])
        actions = {e['action'] for e in entries}
        self.assertTrue({'stage.scheduled', 'stage.started', 'stage.failed', 'stage.skipped', 'run.finished'} <= actions)
        self.assertEqual([e['seq'] for e in entries], list(range(1, len(entries) + 1)))
        self.assertEqual(entries[0]['prev_hash'], '0' * 64)
        self.assertTrue(all(e['actor'] and e['stage_key'] is not None or e['action'].startswith('run.') for e in entries))
        self.assertEqual(json.loads(h.run_row()['evidence_json'])['audit'], {'entries': len(entries), 'head': entries[-1]['hash']})

    def test_append_only_triggers_block_edits(self):
        for sql in ("UPDATE run_audit SET actor='x'", "DELETE FROM run_audit"):
            with self.assertRaises(sqlite3.DatabaseError):
                with self.h.store.tx() as db:
                    db.execute(sql)

    def tamper(self, sql, *args):
        db = self.h.store._db
        db.execute('DROP TRIGGER run_audit_no_update')
        db.execute('DROP TRIGGER run_audit_no_delete')
        db.execute(sql, args)

    def test_tampered_detail_is_detected(self):
        self.tamper("UPDATE run_audit SET detail_json='{}' WHERE seq=3")
        report = self.h.store.verify_audit(self.h.run_id)
        self.assertEqual((report['ok'], report['broken_seq']), (False, 3))

    def test_deleted_entry_is_detected(self):
        self.tamper('DELETE FROM run_audit WHERE seq=4')
        report = self.h.store.verify_audit(self.h.run_id)
        self.assertEqual((report['ok'], report['broken_seq']), (False, 5))

    def test_rewritten_hash_chain_is_detected_by_the_sealed_head(self):
        evidence = json.loads(self.h.run_row()['evidence_json'])['audit']
        self.assertEqual(evidence['entries'], self.h.store.verify_audit(self.h.run_id)['entries'])
        self.assertEqual(evidence['head'], self.h.store.audit(self.h.run_id)[evidence['entries'] - 1]['hash'])

    def test_secret_shaped_text_never_reaches_the_audit_log(self):
        secret = 'sk-' + 'a1B2c3D4e5F6g7H8i9J0k1L2'
        stages = [stage('work'), stage('review', ['work'], role='reviewer')]
        h = Harness(self, stages, script={'review': {'events': [('stage.verdict', {'verdict': 'fail', 'reason': 'leaked ' + secret})]}})
        h.run()
        self.assertNotIn(secret, json.dumps(h.store.audit(h.run_id)))
        self.assertIn('[REDACTED:vendor_key_shape]', json.dumps(h.store.audit(h.run_id)))


class ResumeTests(unittest.TestCase):
    def test_granting_the_approval_resumes_the_blocked_run_from_its_checkpoint(self):
        stages = [stage('a'), stage('b', ['a'], capabilities=['demo.run', 'workspace.write']), stage('c', ['b'])]
        h = Harness(self, stages, script={'b': {'block': 'workspace.write'}}, capabilities=['demo.run', 'workspace.write'])
        self.assertEqual(h.run(), 'done')
        run = h.run_row()
        self.assertEqual((run['status'], run['reason']), ('blocked', 'approval_pending'))
        self.assertEqual(h.attempts('b'), [(1, 'blocked', 'approval_pending')])
        with self.assertRaises(StoreError):
            h.store.resume_run(h.run_id)  # approval still pending
        (approval,) = h.store.list_approvals('pending')
        h.store.decide_approval(approval['id'], 'grant', h.run_id)
        self.assertEqual(h.run_row()['status'], 'queued', 'granting the last pending approval resumes the run')
        h.run(h.claim(owner='owner-b'))
        self.assertEqual(h.run_row()['status'], 'succeeded')
        self.assertEqual(h.attempts('b'), [(1, 'blocked', 'approval_pending'), (2, 'succeeded', None)])
        self.assertEqual(h.executor.executions['a'], 1, 'completed stage not re-executed')
        self.assertEqual(h.executor.views[-2].approved, ('workspace.write',))

    def test_denied_approval_still_fails_the_run(self):
        stages = [stage('b', capabilities=['demo.run', 'workspace.write'])]
        h = Harness(self, stages, script={'b': {'block': 'workspace.write'}}, capabilities=['demo.run', 'workspace.write'])
        h.run()
        (approval,) = h.store.list_approvals('pending')
        h.store.decide_approval(approval['id'], 'deny', h.run_id)
        self.assertEqual((h.run_row()['status'], h.run_row()['reason']), ('failed', 'approval_denied'))

    def test_only_blocked_runs_can_be_resumed(self):
        h = Harness(self, [stage('a')])
        with self.assertRaises(StoreError):
            h.store.resume_run(h.run_id)

    def test_blocked_run_waits_for_running_siblings_then_blocks(self):
        stages = [stage('x', capabilities=['demo.run', 'workspace.write']), stage('y')]
        h = Harness(self, stages, script={'x': {'block': 'workspace.write'}, 'y': {'ticks': 5}},
                    capabilities=['demo.run', 'workspace.write'], limits={'wall_seconds': 600, 'max_workers': 2})
        h.run()
        self.assertEqual(h.stage_status(), {'x': 'blocked', 'y': 'succeeded'})
        self.assertEqual(h.run_row()['status'], 'blocked')


if __name__ == '__main__':
    unittest.main()
