import json
import threading
import time
import unittest

from aether.studio import schema
from aether.studio.runner import Runner
from aether.studio.store import events_sha256

from tests.studio import support
from tests.studio.support import CountingMock, submit, wait_for, wait_status


def types(s, run_id):
    return [(e['type'], e['data'].get('to')) for e in s.events(run_id)[0]]


class SchedulerTests(unittest.TestCase):
    def test_success_fail_cancel_paths(self):
        s = support.store(self)
        support.runner(self, s)
        ok = submit(s, key='a')
        bad = submit(s, 'mock:fail:3', key='b')
        slow = submit(s, 'mock:hang', key='c')
        self.assertEqual(wait_status(s, ok, 'succeeded')['exit_code'], 0)
        failed = wait_status(s, bad, 'failed')
        self.assertEqual((failed['reason'], failed['exit_code']), ('exit_nonzero', 3))
        wait_status(s, slow, 'running')
        self.assertEqual(s.request_cancel(slow), 'cancel_requested')
        done = wait_status(s, slow, 'cancelled')
        self.assertEqual(done['reason'], 'cancelled_by_owner')
        self.assertEqual({st['status'] for st in done['stages']}, {'cancelled'})

    def test_serialized_dispatch_order(self):
        s = support.store(self)
        ids = [submit(s, key=f'k{i}') for i in range(5)]
        support.runner(self, s)
        for run_id in ids:
            wait_status(s, run_id, 'succeeded')
        started = [s.run_row(i)['started_utc'] for i in ids]
        self.assertEqual(started, sorted(started))
        order = []
        for i in ids:
            order.append(next(e['seq'] for e in s.events(i)[0] if e['data'].get('to') == 'running'))
        self.assertTrue(all(o for o in order))
        # with a single worker, each run finishes before the next starts
        ends = [s.run_row(i)['ended_utc'] for i in ids]
        for later_start, earlier_end in zip(started[1:], ends[:-1]):
            self.assertGreaterEqual(later_start, earlier_end)

    def test_concurrency_bound(self):
        for bound in (1, 2):
            with self.subTest(bound=bound):
                s = support.store(self)
                ids = [submit(s, 'mock:sleep:0.3', key=f'k{bound}{i}') for i in range(5)]
                support.runner(self, s, max_workers=bound)
                peak = 0
                end = time.monotonic() + 15
                while time.monotonic() < end:
                    peak = max(peak, s.count('running'))
                    if all(s.run_row(i)['status'] == 'succeeded' for i in ids):
                        break
                    time.sleep(0.005)
                self.assertEqual(s.count('succeeded'), 5)
                self.assertLessEqual(peak, bound)
                self.assertEqual(peak, bound)

    def test_worker_bounds_validated(self):
        s = support.store(self)
        for bad in (0, 5, -1, '1', 1.0):
            with self.assertRaises(ValueError):
                Runner(s, {}, max_workers=bad)
        self.assertEqual(Runner(s, {}).max_workers, 1)
        self.assertEqual(Runner(s, {}, max_workers=4).max_workers, 4)
        with self.assertRaises(ValueError):
            Runner(s, {}, heartbeat_seconds=10, lease_seconds=5)

    def test_no_double_execution(self):
        path = support.tempdir(self) / 'shared.sqlite3'
        s1, s2 = support.store(self, path), support.store(self, path)
        counting = CountingMock()
        ids = [submit(s1, 'mock:sleep:0.05', key=f'k{i}') for i in range(8)]
        support.runner(self, s1, executor=counting, max_workers=2)
        support.runner(self, s2, executor=counting, max_workers=2)
        for run_id in ids:
            wait_status(s1, run_id, 'succeeded')
        self.assertEqual(set(counting.starts), set(ids))
        self.assertEqual(set(counting.starts.values()), {1})

    def test_unhealthy_executor_blocks_instead_of_running(self):
        s = support.store(self)

        class Down(support.MockExecutor):
            def health(self):
                return {'status': 'unavailable', 'observed_utc': '', 'detail': ''}
        run_id = submit(s)
        support.runner(self, s, executor=Down())
        run = wait_status(s, run_id, 'blocked')
        self.assertEqual(run['reason'], 'adapter_unavailable')

    def test_corrupt_snapshot_is_blocked_not_run(self):
        s = support.store(self)
        run_id = submit(s)
        s._db.execute('DROP TRIGGER runs_snapshot_immutable')
        s._db.execute("UPDATE runs SET snapshot_json='{\"mission\": 1}' WHERE id=?", (run_id,))
        counting = CountingMock()
        support.runner(self, s, executor=counting)
        self.assertEqual(wait_status(s, run_id, 'blocked')['reason'], 'invalid_snapshot')
        self.assertFalse(counting.starts)

    def test_stages_run_in_dependency_order_and_failure_blocks_dependents(self):
        s = support.store(self)
        stages = [{'key': 'a', 'title': 'a', 'executor_step': 'mock:success', 'timeout_seconds': 10, 'capabilities': ['demo.run']},
                  {'key': 'b', 'title': 'b', 'needs': ['a'], 'executor_step': 'mock:fail:2', 'timeout_seconds': 10,
                   'capabilities': ['demo.run']},
                  {'key': 'c', 'title': 'c', 'needs': ['b'], 'executor_step': 'mock:success', 'timeout_seconds': 10,
                   'capabilities': ['demo.run']}]
        run_id = submit(s, stages=stages)
        support.runner(self, s)
        run = wait_status(s, run_id, 'failed')
        self.assertEqual({st['key']: st['status'] for st in run['stages']}, {'a': 'succeeded', 'b': 'failed', 'c': 'skipped'})
        self.assertEqual(next(st for st in run['stages'] if st['key'] == 'c')['reason'], 'upstream_failed')

    def test_retry_bounded(self):
        s = support.store(self)
        counting = CountingMock()
        stages = [{'key': 'a', 'title': 'a', 'executor_step': 'mock:fail:1', 'timeout_seconds': 10, 'retry_max': 2,
                   'capabilities': ['demo.run']}]
        run_id = submit(s, stages=stages)
        support.runner(self, s, executor=counting)
        run = wait_status(s, run_id, 'failed')
        self.assertEqual(counting.starts[run_id], 3)
        self.assertEqual(sorted(st['attempt'] for st in run['stages']), [1, 2, 3])

    def test_missing_required_evidence_fails_run(self):
        s = support.store(self)
        spec = schema.demo_spec(outputs=('report',))
        spec['stages'][0]['executor_step'] = 'mock:success'  # declares output but never writes it
        run_id = submit(s, spec=spec)
        support.runner(self, s)
        self.assertEqual(wait_status(s, run_id, 'failed')['reason'], 'executor_error')

    def test_approval_step_blocks_run_with_pending_approval(self):
        s = support.store(self)
        spec = schema.demo_spec('mock:approval:workspace.write')
        spec['capabilities'] = ['demo.run', 'workspace.write']
        spec['stages'][0]['capabilities'] = ['demo.run', 'workspace.write']
        run_id = submit(s, spec=spec)
        support.runner(self, s)
        run = wait_status(s, run_id, 'blocked')
        self.assertEqual(run['reason'], 'approval_pending')
        row = s._db.execute('SELECT status, capability FROM approvals WHERE run_id=?', (run_id,)).fetchone()
        self.assertEqual((row['status'], row['capability']), ('pending', 'workspace.write'))


class LifecycleTests(unittest.TestCase):
    def test_heartbeat_updates(self):
        s = support.store(self)
        run_id = submit(s, 'mock:hang')
        support.runner(self, s)
        first = wait_for(lambda: s.run_row(run_id)['heartbeat_utc'])
        lease_one = s.run_row(run_id)['lease_until']
        wait_for(lambda: s.run_row(run_id)['lease_until'] != lease_one, timeout=5)
        self.assertGreater(s.run_row(run_id)['lease_until'], lease_one)
        self.assertGreaterEqual(s.run_row(run_id)['heartbeat_utc'], first)

    def test_heartbeat_events_are_coalesced(self):
        s = support.store(self)
        run_id = submit(s, 'mock:sleep:0.6')
        support.runner(self, s)
        wait_status(s, run_id, 'succeeded')
        beats = [e for e in s.events(run_id)[0] if e['type'] == 'run.heartbeat']
        self.assertLessEqual(len(beats), 1)

    def test_timeout_marks_failed_reason_timeout(self):
        s = support.store(self)
        run_id = submit(s, 'mock:hang', timeout_seconds=1)
        support.runner(self, s)
        run = wait_status(s, run_id, 'failed', timeout=15)
        self.assertEqual(run['reason'], 'timeout')
        self.assertEqual(run['stages'][0]['status'], 'failed')

    def test_wall_limit_marks_budget_exceeded_with_event(self):
        s = support.store(self)
        run_id = submit(s, 'mock:hang', wall_seconds=1, timeout_seconds=60)
        support.runner(self, s)
        self.assertEqual(wait_status(s, run_id, 'failed', timeout=15)['reason'], 'budget_exceeded')
        self.assertIn('limit.exceeded', [e['type'] for e in s.events(run_id)[0]])

    def test_cancel_queued(self):
        s = support.store(self)
        run_id = submit(s)
        counting = CountingMock()
        r = support.runner(self, s, executor=counting, start=False)
        self.assertEqual(r.cancel(run_id), 'cancelled')
        r.start()
        time.sleep(0.2)
        self.assertEqual(s.get_run(run_id)['status'], 'cancelled')
        self.assertFalse(counting.starts)

    def test_cancel_running_signals_executor(self):
        s = support.store(self)
        counting = CountingMock()
        run_id = submit(s, 'mock:hang')
        r = support.runner(self, s, executor=counting)
        wait_status(s, run_id, 'running')
        wait_for(lambda: counting.starts[run_id])
        self.assertEqual(r.cancel(run_id), 'cancel_requested')
        wait_status(s, run_id, 'cancelled')
        self.assertEqual([c[:2] for c in counting.cancels], [(run_id, 'demo')])

    def test_cancel_terminal_is_noop_409(self):
        from aether.studio.store import TerminalState
        s = support.store(self)
        run_id = submit(s)
        r = support.runner(self, s)
        wait_status(s, run_id, 'succeeded')
        with self.assertRaises(TerminalState):
            r.cancel(run_id)
        self.assertEqual(s.get_run(run_id)['status'], 'succeeded')

    def test_cancelled_run_is_not_started_after_cancel_flag(self):
        s = support.store(self)
        first = submit(s, 'mock:hang', key='1')
        second = submit(s, key='2')
        counting = CountingMock()
        r = support.runner(self, s, executor=counting)
        wait_status(s, first, 'running')
        r.cancel(second)
        r.cancel(first)
        wait_status(s, first, 'cancelled')
        time.sleep(0.2)
        self.assertEqual(s.get_run(second)['status'], 'cancelled')
        self.assertNotIn(second, counting.starts)


class RecoveryTests(unittest.TestCase):
    def test_recovery_expired_lease_with_running_stage_blocks_for_owner(self):
        clock = [1000.0]
        s = support.store(self, clock=lambda: clock[0])
        run_id = submit(s, 'mock:hang')
        row = s.claim_next('dead-owner', lease_seconds=30, max_running=1)
        self.assertEqual(row['id'], run_id)
        self.assertTrue(s.claim_stage(run_id, 'dead-owner', 'demo', 1))  # the stage was running when the process died
        self.assertEqual(s.recover_expired_leases(), {})  # lease still valid
        clock[0] += 31
        counting = CountingMock()
        r = support.runner(self, s, executor=counting, start=False)
        r.recover()
        run = s.get_run(run_id)
        self.assertEqual((run['status'], run['reason']), ('blocked', 'owner_action_required'))
        self.assertEqual([st['status'] for st in run['stages']], ['interrupted'])
        r.start()
        time.sleep(0.3)
        self.assertEqual(s.get_run(run_id)['status'], 'blocked')
        self.assertFalse(counting.starts, 'an interrupted non-restartable stage is never silently re-run')

    def test_recovery_across_real_restart(self):
        path = support.tempdir(self) / 'db.sqlite3'
        s = support.store(self, path)
        run_id = submit(s, 'mock:hang')
        s.claim_next('old-process', lease_seconds=0.05, max_running=1)
        s.claim_stage(run_id, 'old-process', 'demo', 1)
        s.close()
        time.sleep(0.2)
        s2 = support.store(self, path)
        support.runner(self, s2)
        run = wait_status(s2, run_id, 'blocked')
        self.assertEqual(run['reason'], 'owner_action_required')

    def test_recovery_queued_resumes(self):
        path = support.tempdir(self) / 'db.sqlite3'
        s = support.store(self, path)
        run_id = submit(s)
        s.close()
        s2 = support.store(self, path)
        support.runner(self, s2)
        self.assertEqual(wait_status(s2, run_id, 'succeeded')['exit_code'], 0)

    def test_terminal_runs_survive_reopen_unchanged(self):
        path = support.tempdir(self) / 'db.sqlite3'
        s = support.store(self, path)
        r = support.runner(self, s)
        ids = [submit(s, key='a'), submit(s, 'mock:fail:1', key='b')]
        for i in ids:
            wait_status(s, i, 'succeeded', 'failed')
        before = [s.get_run(i) for i in ids]
        r.stop()
        s.close()
        s2 = support.store(self, path)
        support.runner(self, s2)
        time.sleep(0.2)
        self.assertEqual([s2.get_run(i) for i in ids], before)

    def test_live_lease_is_not_stolen_by_second_runner(self):
        path = support.tempdir(self) / 'db.sqlite3'
        s1, s2 = support.store(self, path), support.store(self, path)
        run_id = submit(s1, 'mock:sleep:0.5')
        support.runner(self, s1)
        wait_status(s1, run_id, 'running')
        support.runner(self, s2)
        self.assertEqual(wait_status(s1, run_id, 'succeeded')['reason'], None)


class EvidenceTests(unittest.TestCase):
    def test_evidence_hash_matches_events(self):
        s = support.store(self)
        run_id = submit(s)
        support.runner(self, s)
        run = wait_status(s, run_id, 'succeeded')
        events = s.events(run_id, 0, 200)[0]
        self.assertEqual(run['evidence']['events_sha256'], events_sha256(events))
        self.assertEqual(run['evidence']['snapshot_sha256'], run['snapshot_sha256'])
        self.assertEqual(run['evidence']['exit'], {'status': 'succeeded', 'reason': None, 'exit_code': 0})
        self.assertTrue(run['evidence']['demo'])
        self.assertTrue(all(e['demo'] for e in events))

    def test_evidence_covers_failed_and_cancelled(self):
        s = support.store(self)
        r = support.runner(self, s)
        bad = submit(s, 'mock:fail:5', key='b')
        hang = submit(s, 'mock:hang', key='h')
        wait_status(s, bad, 'failed')
        wait_status(s, hang, 'running')
        r.cancel(hang)
        wait_status(s, hang, 'cancelled')
        for run_id in (bad, hang):
            run = s.get_run(run_id)
            self.assertEqual(run['evidence']['events_sha256'], events_sha256(s.events(run_id, 0, 200)[0]))
        self.assertEqual(s.get_run(bad)['evidence']['exit']['exit_code'], 5)

    def test_artifact_in_evidence_and_on_disk(self):
        s = support.store(self)
        art_dir = support.tempdir(self) / 'artifacts'
        run_id = submit(s, 'mock:artifact:report', outputs=('report',))
        support.runner(self, s, artifacts_dir=art_dir)
        run = wait_status(s, run_id, 'succeeded')
        (art,) = run['evidence']['artifacts']
        self.assertEqual(art['name'], 'report')
        path = art_dir / run_id / art['sha256']
        self.assertEqual(schema.sha256_hex(path.read_bytes()), art['sha256'])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(run['artifacts'][0]['demo'])

    def test_undeclared_artifact_is_refused(self):
        s = support.store(self)
        spec = schema.demo_spec('mock:artifact:sneaky')
        run_id = submit(s, spec=spec)
        support.runner(self, s)
        run = wait_status(s, run_id, 'succeeded')
        self.assertEqual(run['artifacts'], [])
        self.assertIn('artifact_undeclared', json.dumps(s.events(run_id)[0]))

    def test_events_gapless_after_run(self):
        s = support.store(self)
        run_id = submit(s)
        support.runner(self, s)
        wait_status(s, run_id, 'succeeded')
        events, more = s.events(run_id, 0, 200)
        self.assertFalse(more)
        self.assertEqual([e['seq'] for e in events], list(range(1, len(events) + 1)))
        self.assertEqual(events[0]['type'], 'run.submitted')
        self.assertEqual((events[-1]['type'], events[-1]['data']['to']), ('run.status', 'succeeded'))
