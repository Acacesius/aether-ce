import json
import os
import stat
import unittest
from unittest import mock

from aether.studio import schema
from aether.studio.app import StudioServer

from tests.studio import support
from tests.studio.support import wait_for


def demo_body(step='mock:success', **kw):
    body = {'spec': schema.demo_spec(step, **kw), 'name': 'api demo'}
    return body


def idem(key):
    return {'Idempotency-Key': key}


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.app, self.c = support.serve(self)

    def submit(self, step='mock:success', key=None, **kw):
        status, run = self.c.request('POST', '/api/studio/runs', demo_body(step, **kw), headers=idem(key or schema.new_id('run')))
        self.assertEqual(status, 201, run)
        return run['id']

    def wait(self, run_id, *statuses):
        return wait_for(lambda: (lambda r: r if r['status'] in statuses else None)(
            self.c.request('GET', f'/api/studio/runs/{run_id}')[1]))

    def test_auth_required(self):
        for method, path in (('GET', '/api/studio/runs'), ('GET', '/api/studio/mode'), ('POST', '/api/studio/runs'),
                             ('GET', '/api/studio/capabilities'), ('GET', '/api/studio/unknown')):
            status, body = self.c.request(method, path, {} if method == 'POST' else None, auth=False)
            self.assertEqual((status, body), (401, {'error': 'owner_auth_required'}), path)
        status, _ = self.c.request('GET', '/api/studio/runs', headers={'Authorization': 'Bearer wrong'})
        self.assertEqual(status, 401)
        status, _ = self.c.request('GET', '/api/studio/runs', auth=False, headers={'Cookie': 'owner=guess'})
        self.assertEqual(status, 401)

    def test_host_origin_and_site_guards(self):
        self.assertEqual(self.c.request('GET', '/healthz', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.c.request('POST', '/api/studio/runs', demo_body(), headers={**idem('k'), 'Origin': 'http://evil.example'})[0], 403)
        self.assertEqual(self.c.request('GET', '/api/studio/runs', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)

    def test_healthz_needs_no_token_and_reveals_no_state(self):
        status, body = self.c.request('GET', '/healthz', auth=False)
        self.assertEqual((status, body), (200, {'status': 'ok', 'mode': 'custom', 'demo_only': True}))

    def test_mode_and_capabilities(self):
        status, body = self.c.request('GET', '/api/studio/mode')
        self.assertEqual(status, 200)
        self.assertEqual((body['mode'], body['provider'], body['runtime'], body['demo_only']), ('custom', 'none', 'docker', True))
        self.assertEqual(body['executors']['mock']['status'], 'ready')
        self.assertEqual(body['executors']['adapter']['status'], 'unavailable')
        caps = {c['id']: c for c in self.c.request('GET', '/api/studio/capabilities')[1]['capabilities']}
        self.assertTrue(caps['demo.run']['available'])
        self.assertFalse(caps['net.egress.allowlist']['available'])
        self.assertFalse(any(k.startswith(('shell', 'ssh', 'docker', 'provider.cloud')) for k in caps))

    def test_submit_runs_and_labels_demo(self):
        run_id = self.submit()
        run = self.wait(run_id, 'succeeded')
        self.assertEqual((run['demo'], run['label']), (True, 'DEMO'))
        listing = self.c.request('GET', '/api/studio/runs')[1]['runs']
        self.assertEqual([r['id'] for r in listing], [run_id])
        self.assertEqual(listing[0]['label'], 'DEMO')
        self.assertEqual(self.c.request('GET', '/api/studio/runs?status=failed')[1], {'runs': []})

    def test_fail_and_cancel_via_api(self):
        bad = self.submit('mock:fail:4')
        self.assertEqual(self.wait(bad, 'failed')['exit_code'], 4)
        hang = self.submit('mock:hang')
        self.wait(hang, 'running')
        status, body = self.c.request('POST', f'/api/studio/runs/{hang}/cancel', {})
        self.assertEqual((status, body['result']), (202, 'cancel_requested'))
        self.assertEqual(self.wait(hang, 'cancelled')['reason'], 'cancelled_by_owner')

    def test_cancel_terminal_is_409(self):
        run_id = self.submit()
        self.wait(run_id, 'succeeded')
        status, body = self.c.request('POST', f'/api/studio/runs/{run_id}/cancel', {})
        self.assertEqual((status, body['error']), (409, 'terminal_state'))

    def test_cancel_body_is_strict(self):
        run_id = self.submit('mock:hang')
        self.assertEqual(self.c.request('POST', f'/api/studio/runs/{run_id}/cancel', {'x': 1})[0], 400)
        self.assertEqual(self.c.request('POST', f'/api/studio/runs/{run_id}/cancel', {'confirm': run_id})[0], 202)

    def test_idempotent_submit(self):
        body = demo_body()
        s1, r1 = self.c.request('POST', '/api/studio/runs', body, headers=idem('same-key'))
        s2, r2 = self.c.request('POST', '/api/studio/runs', body, headers=idem('same-key'))
        self.assertEqual((s1, s2, r1['id'] == r2['id'], r2['idempotent_replay']), (201, 200, True, True))
        s3, r3 = self.c.request('POST', '/api/studio/runs', demo_body('mock:fail:1'), headers=idem('same-key'))
        self.assertEqual((s3, r3['error']), (409, 'idempotency_conflict'))
        self.assertEqual(len(self.c.request('GET', '/api/studio/runs')[1]['runs']), 1)

    def test_idempotency_key_required_and_validated(self):
        for headers in ({}, idem('bad key!'), idem('x' * 129)):
            status, body = self.c.request('POST', '/api/studio/runs', demo_body(), headers=headers)
            self.assertEqual((status, body['error']), (400, 'invalid_request'))

    def test_events_cursor(self):
        run_id = self.submit()
        self.wait(run_id, 'succeeded')
        status, page = self.c.request('GET', f'/api/studio/runs/{run_id}/events?limit=2')
        self.assertEqual((status, len(page['events']), page['has_more'], page['next_cursor']), (200, 2, True, 2))
        self.assertEqual(page['poll_after_ms'], 500)
        seen = [e['seq'] for e in page['events']]
        cursor = page['next_cursor']
        while True:
            status, page = self.c.request('GET', f'/api/studio/runs/{run_id}/events?cursor={cursor}&limit=2')
            seen += [e['seq'] for e in page['events']]
            cursor = page['next_cursor']
            if not page['has_more']:
                break
        self.assertEqual(seen, list(range(1, len(seen) + 1)))
        self.assertEqual(page['poll_after_ms'], 0)
        status, tail = self.c.request('GET', f'/api/studio/runs/{run_id}/events?cursor={cursor}')
        self.assertEqual((tail['events'], tail['next_cursor']), ([], cursor))
        self.assertTrue(all(e['demo'] for e in page['events']))

    def test_bad_query_params(self):
        run_id = self.submit()
        for q in ('limit=0', 'limit=201', 'limit=abc', 'cursor=-1', 'limit=1&limit=2'):
            self.assertEqual(self.c.request('GET', f'/api/studio/runs/{run_id}/events?{q}')[0], 400, q)
        self.assertEqual(self.c.request('GET', '/api/studio/runs?status=bogus')[0], 400)

    def test_evidence_endpoint(self):
        run_id = self.submit()
        self.wait(run_id, 'succeeded')
        status, ev = self.c.request('GET', f'/api/studio/runs/{run_id}/evidence')
        self.assertEqual(status, 200)
        self.assertTrue(ev['sealed'] and ev['demo'] and ev['label'] == 'DEMO')
        self.assertRegex(ev['events_sha256'], r'^[0-9a-f]{64}$')

    def test_unknown_run_404(self):
        for suffix in ('', '/events', '/evidence'):
            self.assertEqual(self.c.request('GET', '/api/studio/runs/run_' + '0' * 26 + suffix)[0], 404)
        self.assertEqual(self.c.request('POST', '/api/studio/runs/run_' + '0' * 26 + '/cancel', {})[0], 404)
        self.assertEqual(self.c.request('GET', '/api/studio/runs/not-an-id')[0], 404)

    def test_schema_errors_are_400_and_capability_403(self):
        body = demo_body()
        body['spec']['capabilities'] = ['demo.run', 'shell.host']
        status, err = self.c.request('POST', '/api/studio/runs', body, headers=idem('k1'))
        self.assertEqual((status, err['error']), (403, 'capability_not_allowed'))
        body = demo_body()
        body['spec']['junk'] = 1
        status, err = self.c.request('POST', '/api/studio/runs', body, headers=idem('k2'))
        self.assertEqual((status, err['error']), (400, 'unknown_field'))
        status, err = self.c.request('POST', '/api/studio/runs', {**demo_body(), 'overrides': {'nope': 1}}, headers=idem('k3'))
        self.assertEqual((status, err['error']), (400, 'override_not_allowed'))
        status, err = self.c.request('POST', '/api/studio/runs', {'spec': {}, 'version_id': 'x'}, headers=idem('k4'))
        self.assertEqual(status, 400)
        self.assertEqual(self.c.request('GET', '/api/studio/runs')[1], {'runs': []})

    def test_body_rules(self):
        h = idem('kb')
        self.assertEqual(self.c.request('POST', '/api/studio/runs', '{"a":1,"a":2}', headers=h)[1]['error'], 'duplicate_field')
        self.assertEqual(self.c.request('POST', '/api/studio/runs', '[]', headers=h)[1]['error'], 'invalid_request')
        self.assertEqual(self.c.request('POST', '/api/studio/runs', 'x' * (256 * 1024 + 1), headers=h)[0], 413)
        self.assertEqual(self.c.request('POST', '/api/studio/runs', '{}', headers={**h, 'Content-Type': 'text/plain'})[0], 415)

    def test_relaunch_references_prior_run(self):
        first = self.submit()
        self.wait(first, 'succeeded')
        status, again = self.c.request('POST', '/api/studio/runs', {**demo_body(), 'relaunch_of': first}, headers=idem('re1'))
        self.assertEqual((status, again['relaunch_of']), (201, first))
        status, err = self.c.request('POST', '/api/studio/runs', {**demo_body(), 'relaunch_of': 'run_' + '0' * 26}, headers=idem('re2'))
        self.assertEqual(status, 404)

    def test_queue_full_429(self):
        self.app.runner.stop()
        for i in range(100):
            self.app.store.submit(self.app.store.ensure_inline_version('q', schema.validate_spec(schema.demo_spec('mock:hang'))),
                                  schema.validate_spec(schema.demo_spec('mock:hang')), {}, idempotency_key=f'q{i}',
                                  request_sha256='x')
        status, err = self.c.request('POST', '/api/studio/runs', demo_body(), headers=idem('over'))
        self.assertEqual((status, err['error']), (429, 'queue_full'))

    def test_responses_have_security_headers_and_no_store(self):
        import http.client
        conn = http.client.HTTPConnection('127.0.0.1', self.c.port, timeout=5)
        conn.request('GET', '/healthz', headers={'Host': f'127.0.0.1:{self.c.port}'})
        headers = {k.lower(): v for k, v in conn.getresponse().getheaders()}
        conn.close()
        self.assertEqual(headers['cache-control'], 'no-store')
        self.assertEqual(headers['x-content-type-options'], 'nosniff')
        self.assertIn("default-src 'none'", headers['content-security-policy'])

    def test_unsupported_methods_and_paths(self):
        for method in ('DELETE', 'PATCH'):
            self.assertEqual(self.c.request(method, '/api/studio/runs')[0], 405)
        self.assertEqual(self.c.request('PUT', '/api/studio/runs', {})[0], 404)  # PUT exists only for definitions
        self.assertEqual(self.c.request('GET', '/api/jobs')[0], 404)  # live routes do not exist in CE
        self.assertEqual(self.c.request('GET', '/api/studio/runs/x/../..')[0], 404)

    def test_owner_token_is_private_file(self):
        token = self.app.mode.data_dir / 'owner.token'
        self.assertEqual(stat.S_IMODE(token.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.app.mode.data_dir.stat().st_mode), 0o700)
        self.assertTrue((self.app.mode.data_dir / 'studio.sqlite3').exists())


class BindTests(unittest.TestCase):
    def test_refuses_non_loopback_outside_docker(self):
        from aether.studio.app import build
        data = support.tempdir(self)
        os.chmod(data, 0o700)
        app = build(support.ce_mode(data))
        self.addCleanup(app.store.close)
        with self.assertRaises(ValueError):
            StudioServer(('0.0.0.0', 0), app, dockerenv=data / 'no-dockerenv')
        marker = data / 'dockerenv'
        marker.touch()
        httpd = StudioServer(('0.0.0.0', 0), app, dockerenv=marker)
        httpd.server_close()

    def test_refuses_admin_token_env(self):
        from aether.security import SecurityError
        from aether.studio.app import build
        data = support.tempdir(self)
        os.chmod(data, 0o700)
        app = build(support.ce_mode(data))
        self.addCleanup(app.store.close)
        with mock.patch.dict(os.environ, {'AETHER_ADMIN_TOKEN': 'x'}), self.assertRaises(SecurityError):
            StudioServer(('127.0.0.1', 0), app)

    def test_application_requires_custom_mode(self):
        from aether.studio.app import StudioApplication
        from aether.studio.mode import Mode, ModeError
        from pathlib import Path
        with self.assertRaises(ModeError):
            StudioApplication(Mode('live', 'none', 'host', Path('.')), store=None, runner=None, auth=None, executors={}, assist=None)
