"""AHS-02.7: library CRUD API, concurrent submissions, idempotency, approvals."""
import threading
import unittest

from aether.studio import schema

from tests.studio import support
from tests.studio.support import wait_for

D = '/api/studio/definitions'


def spec(mission='DEMO: lib', step='mock:success'):
    s = schema.demo_spec(step)
    s['mission'] = mission
    s['inputs'] = [{'name': 'topic', 'type': 'string', 'default': 'alpha'}]
    return s


class LibraryApiTests(unittest.TestCase):
    def setUp(self):
        self.app, self.c = support.serve(self)

    def create(self, name='lib', **kw):
        status, d = self.c.request('POST', D, {'name': name, 'description': 'd', 'spec': spec(**kw)})
        self.assertEqual(status, 201, d)
        return d

    def test_crud_roundtrip(self):
        d = self.create()
        self.assertEqual((d['rev'], d['latest_version'], d['archived']), (0, 1, False))
        self.assertEqual(self.c.request('GET', D)[1]['definitions'][0]['id'], d['id'])
        status, got = self.c.request('GET', f'{D}/{d["id"]}')
        self.assertEqual((status, len(got['versions'])), (200, 1))
        status, upd = self.c.request('PUT', f'{D}/{d["id"]}', {'spec': spec('DEMO: edited')}, headers={'If-Match': '0'})
        self.assertEqual((status, upd['rev'], upd['latest_version']), (200, 1, 2))
        status, stale = self.c.request('PUT', f'{D}/{d["id"]}', {'name': 'x'}, headers={'If-Match': '0'})
        self.assertEqual((status, stale['error']), (409, 'rev_conflict'))
        self.assertEqual(self.c.request('PUT', f'{D}/{d["id"]}', {'name': 'x'})[0], 400)  # If-Match required
        self.assertEqual(self.c.request('PUT', f'{D}/{d["id"]}', {}, headers={'If-Match': '1'})[0], 400)
        status, ver = self.c.request('GET', f'/api/studio/versions/{got["versions"][0]["id"]}')
        self.assertEqual((status, ver['spec']['mission']), (200, 'DEMO: lib'))
        self.assertEqual(self.c.request('GET', f'{D}/hd_{"0" * 26}')[0], 404)

    def test_create_validation(self):
        self.assertEqual(self.c.request('POST', D, {'name': 'x'})[0], 400)
        self.assertEqual(self.c.request('POST', D, {'name': 'x', 'spec': spec(), 'extra': 1})[1]['error'], 'unknown_field')
        bad = spec()
        bad['capabilities'] = ['shell.exec']
        self.assertEqual(self.c.request('POST', D, {'name': 'x', 'spec': bad})[0], 403)
        self.assertEqual(self.c.request('POST', D, {'name': '', 'spec': spec()})[0], 400)

    def test_archive_hides_not_deletes(self):
        d = self.create()
        status, a = self.c.request('POST', f'{D}/{d["id"]}/archive', {})
        self.assertEqual((status, a['archived']), (200, True))
        self.assertEqual(self.c.request('GET', D)[1]['definitions'], [])
        self.assertEqual(len(self.c.request('GET', D + '?archived=1')[1]['definitions']), 1)
        status, got = self.c.request('GET', f'{D}/{d["id"]}')
        self.assertEqual((status, got['archived'], len(got['versions'])), (200, True, 1))
        self.assertEqual(self.c.request('PUT', f'{D}/{d["id"]}', {'name': 'n'}, headers={'If-Match': str(a['rev'])})[0], 409)
        status, run = self.c.request('POST', '/api/studio/runs', {'definition_id': d['id']}, headers={'Idempotency-Key': 'arch-1'})
        self.assertEqual(status, 404, 'archived definitions cannot start new runs')
        status, back = self.c.request('POST', f'{D}/{d["id"]}/unarchive', {})
        self.assertEqual((status, back['archived']), (200, False))
        self.assertEqual(len(self.c.request('GET', D)[1]['definitions']), 1)
        self.assertEqual(self.c.request('POST', f'{D}/{d["id"]}/archive', {'x': 1})[0], 400)
        self.assertEqual(self.c.request('POST', f'{D}/{d["id"]}/archive', {}, headers={'If-Match': '99'})[0], 409)

    def test_submit_from_definition_with_override_and_frozen_snapshot(self):
        d = self.create()
        status, run = self.c.request('POST', '/api/studio/runs', {'definition_id': d['id'], 'overrides': {'topic': 'beta'}},
                                     headers={'Idempotency-Key': 'lib-1'})
        self.assertEqual(status, 201, run)
        self.assertEqual(run['snapshot']['inputs'][0]['default'], 'beta')
        self.assertEqual(run['overrides'], {'topic': 'beta'})
        self.assertTrue(run['demo'])
        self.c.request('PUT', f'{D}/{d["id"]}', {'spec': spec('DEMO: after')}, headers={'If-Match': '0'})
        again = self.c.request('GET', f'/api/studio/runs/{run["id"]}')[1]
        self.assertEqual(again['snapshot'], run['snapshot'])
        self.assertEqual(again['snapshot_sha256'], run['snapshot_sha256'])
        status, bad = self.c.request('POST', '/api/studio/runs', {'definition_id': d['id'], 'overrides': {'mission': 'x'}},
                                     headers={'Idempotency-Key': 'lib-2'})
        self.assertEqual((status, bad['error']), (400, 'override_not_allowed'))
        status, both = self.c.request('POST', '/api/studio/runs', {'definition_id': d['id'], 'spec': spec()},
                                      headers={'Idempotency-Key': 'lib-3'})
        self.assertEqual(status, 400)

    def test_concurrent_submissions_distinct_runs(self):
        d = self.create()
        ids, errors = [], []
        barrier = threading.Barrier(8)

        def go(i):
            barrier.wait()
            status, run = self.c.request('POST', '/api/studio/runs', {'definition_id': d['id']},
                                         headers={'Idempotency-Key': f'conc-{i}'})
            (ids if status == 201 else errors).append(run['id'] if status == 201 else (status, run))
        threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(len(set(ids)), 8)

    def test_concurrent_same_idempotency_key_creates_one_run(self):
        d = self.create()
        out = []
        barrier = threading.Barrier(6)

        def go():
            barrier.wait()
            out.append(self.c.request('POST', '/api/studio/runs', {'definition_id': d['id']},
                                      headers={'Idempotency-Key': 'same-key'}))
        threads = [threading.Thread(target=go) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(s for s, _ in out), [200] * 5 + [201])
        self.assertEqual(len({r['id'] for _, r in out}), 1)
        status, conflict = self.c.request('POST', '/api/studio/runs', {'definition_id': d['id'], 'overrides': {'topic': 'z'}},
                                          headers={'Idempotency-Key': 'same-key'})
        self.assertEqual((status, conflict['error']), (409, 'idempotency_conflict'))

    def test_concurrent_updates_over_http_one_wins(self):
        d = self.create()
        statuses = []
        barrier = threading.Barrier(6)

        def go(i):
            barrier.wait()
            statuses.append(self.c.request('PUT', f'{D}/{d["id"]}', {'spec': spec(f'DEMO: racer {i}')},
                                           headers={'If-Match': '0'})[0])
        threads = [threading.Thread(target=go, args=(i,)) for i in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(statuses), [200] + [409] * 5)
        self.assertEqual(self.c.request('GET', f'{D}/{d["id"]}')[1]['latest_version'], 2)


class ApprovalApiTests(unittest.TestCase):
    def setUp(self):
        self.app, self.c = support.serve(self)

    def blocked_run(self, key):
        s = schema.demo_spec('mock:approval:workspace.write')
        s['capabilities'] = ['demo.run', 'workspace.write']
        s['stages'][0]['capabilities'] = ['demo.run', 'workspace.write']
        status, run = self.c.request('POST', '/api/studio/runs', {'spec': s, 'name': key}, headers={'Idempotency-Key': key})
        self.assertEqual(status, 201, run)
        wait_for(lambda: self.c.request('GET', '/api/studio/approvals?status=pending')[1]['approvals'] and
                 self.c.request('GET', f'/api/studio/runs/{run["id"]}')[1]['status'] == 'blocked')
        apr = [a for a in self.c.request('GET', '/api/studio/approvals?status=pending')[1]['approvals']
               if a['run_id'] == run['id']][0]
        return run['id'], apr['id']

    def test_grant_once(self):
        run_id, apr = self.blocked_run('apr-grant')
        path = f'/api/studio/approvals/{apr}'
        self.assertEqual(self.c.request('POST', path, {'decision': 'grant'})[0], 400)
        self.assertEqual(self.c.request('POST', path, {'decision': 'grant', 'confirm': 'run_wrong'})[0], 400)
        self.assertEqual(self.c.request('POST', path, {'decision': 'maybe', 'confirm': run_id})[0], 400)
        status, got = self.c.request('POST', path, {'decision': 'grant', 'confirm': run_id})
        self.assertEqual((status, got['status'], got['decided_by']), (200, 'granted', 'owner'))
        status, again = self.c.request('POST', path, {'decision': 'deny', 'confirm': run_id})
        self.assertEqual((status, again['error']), (409, 'rev_conflict'))
        events = self.c.request('GET', f'/api/studio/runs/{run_id}/events')[1]['events']
        self.assertIn('approval.decided', [e['type'] for e in events])
        # granting the last pending approval resumes the blocked run from its checkpoint (AHS-05)
        support.wait_for(lambda: self.c.request('GET', f'/api/studio/runs/{run_id}')[1]['status'] == 'succeeded')

    def test_deny_fails_blocked_run(self):
        run_id, apr = self.blocked_run('apr-deny')
        status, got = self.c.request('POST', f'/api/studio/approvals/{apr}', {'decision': 'deny', 'confirm': run_id})
        self.assertEqual((status, got['status']), (200, 'denied'))
        run = self.c.request('GET', f'/api/studio/runs/{run_id}')[1]
        self.assertEqual((run['status'], run['reason']), ('failed', 'approval_denied'))
        self.assertEqual(self.c.request('POST', f'/api/studio/approvals/apr_{"0" * 26}', {'decision': 'grant', 'confirm': run_id})[0], 404)

    def test_concurrent_decisions_only_one_wins(self):
        run_id, apr = self.blocked_run('apr-race')
        statuses = []
        barrier = threading.Barrier(6)

        def go(i):
            barrier.wait()
            statuses.append(self.c.request('POST', f'/api/studio/approvals/{apr}',
                                           {'decision': 'grant' if i % 2 else 'deny', 'confirm': run_id})[0])
        threads = [threading.Thread(target=go, args=(i,)) for i in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(statuses), [200] + [409] * 5)


if __name__ == '__main__':
    unittest.main()
