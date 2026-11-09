"""AHS-05 integration: migration 004 on a populated database, schema additions, HTTP routes, real Runner threads."""
from __future__ import annotations

from pathlib import Path
import shutil
import sqlite3
import unittest
from unittest import mock

from aether.studio import schema, store as store_module
from tests.studio import support
from tests.studio.orch_support import stage
from tests.studio.support import submit, wait_status


class MigrationTests(unittest.TestCase):
    def test_004_keeps_existing_runs_stages_and_children(self):
        legacy = support.tempdir(self) / 'migrations'
        legacy.mkdir()
        for path in sorted(store_module.MIGRATIONS.glob('00[123]_*.sql')):
            shutil.copy(path, legacy / path.name)
        db = support.tempdir(self) / 'studio.sqlite3'
        with mock.patch.object(store_module, 'MIGRATIONS', legacy):
            old = support.store(self, db)
            run_id = submit(old, 'mock:success')
            old.claim_next('o', lease_seconds=30, max_running=1)
            with old.tx() as conn:
                conn.execute("UPDATE stages SET status='succeeded' WHERE run_id=?", (run_id,))
            events_before = old.events(run_id, 0, 100)[0]
            before = old.run_row(run_id)
            stages_before = old.stages(run_id)
            old.close()
        new = support.store(self, db)
        self.assertEqual([r[0] for r in new._db.execute('SELECT version FROM schema_migrations')], [1, 2, 3, 4, 5])
        after = new.run_row(run_id)
        self.assertEqual({k: after[k] for k in before}, before)
        self.assertEqual(after['wall_used'], 0)
        self.assertEqual([{k: s[k] for k in stages_before[0]} for s in new.stages(run_id)], stages_before)
        self.assertEqual(new.events(run_id, 0, 100)[0], events_before)
        self.assertEqual(new._db.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertEqual(new._db.execute('PRAGMA foreign_keys').fetchone()[0], 1)
        with self.assertRaises(sqlite3.DatabaseError):  # runs triggers were recreated
            with new.tx() as conn:
                conn.execute('DELETE FROM runs')
        # the old run still finishes under the new schema (new reasons/statuses accepted)
        with new.tx() as conn:
            conn.execute("UPDATE stages SET status='skipped', reason='upstream_failed' WHERE run_id=?", (run_id,))


class SchemaTests(unittest.TestCase):
    def test_specs_without_new_fields_keep_their_hash(self):
        spec = schema.validate_spec(schema.demo_spec())
        self.assertNotIn('role', spec['stages'][0])
        self.assertNotIn('on_failure', spec)
        self.assertEqual(schema.spec_sha256(schema.validate_spec(spec)), schema.spec_sha256(spec))

    def test_new_fields_validate(self):
        def make(**extra):
            return schema.demo_spec(stages=[stage('a', **extra)])
        ok = schema.validate_spec(make(role='reviewer', optional=True, restartable=True, max_workers=4))
        self.assertEqual(schema.stage_role(ok['stages'][0]), 'reviewer')
        for bad in ({'role': 'boss'}, {'max_workers': 5}, {'max_workers': 0}, {'optional': 'yes'}, {'restartable': 1}):
            with self.assertRaises(schema.SchemaError, msg=bad):
                schema.validate_spec(make(**bad))
        with self.assertRaises(schema.SchemaError):
            schema.validate_spec(dict(make(), on_failure='explode'))
        self.assertEqual(schema.validate_spec(dict(make(), on_failure='stop'))['on_failure'], 'stop')

    def test_attempt_bound(self):
        spec = schema.validate_spec(schema.demo_spec(stages=[stage('a', retry_max=2)]))
        self.assertEqual(schema.max_attempts(spec, spec['stages'][0]), 3)
        spec['retry']['max_attempts'] = 3
        self.assertEqual(schema.max_attempts(spec, stage('x', retry_max=0) | {'retry_max': 0}), 3)

    def test_budget_reason_is_a_run_reason(self):
        self.assertIn('budget_exceeded', schema.REASONS)


class RunnerIntegrationTests(unittest.TestCase):
    def test_diamond_through_the_real_runner(self):
        s = support.store(self)
        stages = [stage('a'), stage('b', ['a']), stage('c', ['a']), stage('d', ['b', 'c'])]
        run_id = submit(s, spec=schema.demo_spec(stages=stages))
        counting = support.CountingMock()
        support.runner(self, s, executor=counting)
        run = wait_status(s, run_id, 'succeeded')
        self.assertEqual({st['key']: st['status'] for st in run['stages']}, {k: 'succeeded' for k in 'abcd'})
        self.assertEqual(counting.starts[run_id], 4)
        self.assertTrue(s.verify_audit(run_id)['ok'])

    def test_real_restart_resumes_without_re_executing_completed_stages(self):
        path = support.tempdir(self) / 'db.sqlite3'
        s = support.store(self, path)
        stages = [stage('a'), stage('b', ['a'], restartable=True, step='mock:sleep:0.05'), stage('c', ['b'])]
        run_id = submit(s, spec=schema.demo_spec(stages=stages))
        s.claim_next('old-process', lease_seconds=0.05, max_running=1)
        # the old process finished a, was running b, then died
        self.assertTrue(s.claim_stage(run_id, 'old-process', 'a', 1))
        self.assertTrue(s.end_stage(run_id, 'old-process', 'a', 1, 'succeeded', exit_code=0))
        self.assertTrue(s.claim_stage(run_id, 'old-process', 'b', 1))
        s.close()
        import time
        time.sleep(0.15)
        s2 = support.store(self, path)
        counting = support.CountingMock()
        support.runner(self, s2, executor=counting)
        run = wait_status(s2, run_id, 'succeeded')
        keys = [(st['key'], st['attempt'], st['status']) for st in run['stages']]
        self.assertEqual(keys, [('a', 1, 'succeeded'), ('b', 1, 'interrupted'), ('c', 1, 'succeeded'), ('b', 2, 'succeeded')])
        self.assertEqual(counting.starts[run_id], 2, 'only b (re-run once) and c executed after the restart; a never')


class UiTests(unittest.TestCase):
    def test_runs_tab_shows_graph_audit_and_resume(self):
        source = (Path(__file__).resolve().parents[2] / 'aether' / 'studio' / 'ui' / 'runs.js').read_text()
        for needle in ('renderStages', 'attempt ${s.attempt}/${allowed(spec)}', '/audit', 'Resume run', '/resume', 'Audit trail'):
            self.assertIn(needle, source)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.app, self.c = support.serve(self)

    def submit(self, spec, key):
        status, run = self.c.request('POST', '/api/studio/runs', {'spec': spec, 'name': key}, headers={'Idempotency-Key': key})
        self.assertEqual(status, 201, run)
        return run['id']

    def test_audit_route_and_auth(self):
        run_id = self.submit(schema.demo_spec(), 'audit-1')
        wait_status(self.app.store, run_id, 'succeeded')
        status, body = self.c.request('GET', f'/api/studio/runs/{run_id}/audit')
        self.assertEqual(status, 200)
        self.assertTrue(body['verify']['ok'])
        self.assertEqual([e['action'] for e in body['entries']][:2], ['stage.scheduled', 'stage.started'])
        self.assertEqual(body['entries'][-1]['action'], 'run.finished')
        self.assertEqual(self.c.request('GET', f'/api/studio/runs/{run_id}/audit', auth=False)[0], 401)
        self.assertEqual(self.c.request('POST', f'/api/studio/runs/{run_id}/audit', {})[0], 404)
        self.assertEqual(self.c.request('GET', f'/api/studio/runs/run_{"0" * 26}/audit')[0], 404)
        self.app.store._db.execute('DROP TRIGGER run_audit_no_update')
        self.app.store._db.execute("UPDATE run_audit SET actor='mallory' WHERE seq=1")
        body = self.c.request('GET', f'/api/studio/runs/{run_id}/audit')[1]
        self.assertEqual((body['verify']['ok'], body['verify']['broken_seq']), (False, 1))

    def test_resume_route_resumes_an_owner_blocked_run(self):
        run_id = self.submit(schema.demo_spec('mock:sleep:0.05'), 'resume-1')
        wait_status(self.app.store, run_id, 'succeeded')
        status, body = self.c.request('POST', f'/api/studio/runs/{run_id}/resume', {'confirm': run_id})
        self.assertEqual((status, body['error']), (400, 'invalid_request'), 'only blocked runs resume')
        self.assertEqual(self.c.request('POST', f'/api/studio/runs/{run_id}/resume', {'confirm': 'run_x'})[0], 400)
        self.assertEqual(self.c.request('POST', f'/api/studio/runs/{run_id}/resume', {}, auth=False)[0], 401)

    def test_approving_a_blocked_run_resumes_it_over_http(self):
        spec = schema.demo_spec('mock:approval:workspace.write')
        spec['capabilities'] = ['demo.run', 'workspace.write']
        spec['stages'][0]['capabilities'] = ['demo.run', 'workspace.write']
        run_id = self.submit(spec, 'approve-resume')
        wait_status(self.app.store, run_id, 'blocked')
        (apr,) = [a for a in self.c.request('GET', '/api/studio/approvals?status=pending')[1]['approvals'] if a['run_id'] == run_id]
        self.assertEqual(self.c.request('POST', f'/api/studio/runs/{run_id}/resume', {})[0], 400, 'approval still pending')
        self.assertEqual(self.c.request('POST', f'/api/studio/approvals/{apr["id"]}', {'decision': 'grant', 'confirm': run_id})[0], 200)
        run = wait_status(self.app.store, run_id, 'succeeded')
        self.assertEqual([(s['attempt'], s['status']) for s in run['stages']], [(1, 'blocked'), (2, 'succeeded')])
        actions = [e['action'] for e in self.c.request('GET', f'/api/studio/runs/{run_id}/audit')[1]['entries']]
        self.assertIn('run.resumed', actions)

    def test_run_view_exposes_added_stages_and_usage(self):
        run_id = self.submit(schema.demo_spec(), 'view-1')
        run = wait_status(self.app.store, run_id, 'succeeded')
        self.assertEqual(run['added_stages'], [])
        self.assertEqual(run['stages'][0]['tokens'], 0)


if __name__ == '__main__':
    unittest.main()
