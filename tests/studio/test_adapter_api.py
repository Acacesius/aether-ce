"""AHS-04: least-privilege enrollment over the owner API, capability report, export exclusion, store immutability."""
import json
import os
import shutil
import sqlite3
import unittest

from aether.studio import adapter_protocol, schema
from aether.studio.executors.adapter import validate_enrollment
from aether.studio.schema import SchemaError

from tests.studio import support


class EnrollmentApiTests(unittest.TestCase):
    def setUp(self):
        self.app, self.c = support.serve(self)
        self.dir = support.install_adapters(self)
        self.echo = str(self.dir / 'echo_adapter.py')

    def enroll(self, **body):
        return self.c.request('POST', '/api/studio/adapters', dict({'name': 'echo', 'script': self.echo}, **body))

    def test_enroll_grants_nothing_by_default(self):
        status, adapter = self.enroll(args=['--quiet'])
        self.assertEqual(status, 201, adapter)
        self.assertRegex(adapter['id'], r'^adp_[0-9A-HJKMNP-TV-Z]{26}$')
        self.assertEqual((adapter['granted'], adapter['revoked'], adapter['args']), ([], False, ['--quiet']))
        self.assertEqual(adapter['health']['status'], 'unavailable')
        self.assertEqual(adapter['health']['detail'], 'observe not granted')
        self.assertEqual(self.enroll()[1]['error'], 'invalid_request')        # one active enrollment per name

    def test_grant_needs_confirm_and_known_scopes(self):
        adapter = self.enroll()[1]
        path = f'/api/studio/adapters/{adapter["id"]}/grant'
        for body in ({'scopes': ['submit']}, {'scopes': ['submit'], 'confirm': 'adp_x'},
                     {'scopes': ['shell'], 'confirm': adapter['id']}, {'scopes': ['observe', 'observe'], 'confirm': adapter['id']},
                     {'scopes': 'observe', 'confirm': adapter['id']}):
            with self.subTest(body=body):
                status, error = self.c.request('POST', path, body)
                self.assertEqual((status, error['error']), (400, 'invalid_request'))
        status, granted = self.c.request('POST', path, {'scopes': ['observe', 'submit'], 'confirm': adapter['id']})
        self.assertEqual((status, granted['granted']), (200, ['observe', 'submit']))
        status, probed = self.c.request('POST', f'/api/studio/adapters/{adapter["id"]}/probe', {})
        self.assertEqual((status, probed['health']['status']), (200, 'ready'), probed)
        self.assertEqual(probed['health']['scopes'], ['observe', 'submit'])

    def test_capabilities_and_mode_list_enrolled_adapters(self):
        adapter = self.enroll()[1]
        self.c.request('POST', f'/api/studio/adapters/{adapter["id"]}/grant', {'scopes': ['observe'], 'confirm': adapter['id']})
        self.c.request('POST', f'/api/studio/adapters/{adapter["id"]}/probe', {})
        status, caps = self.c.request('GET', '/api/studio/capabilities')
        self.assertEqual(status, 200)
        self.assertEqual(caps['adapter_protocol'], 'aether.studio.adapter/v1')
        self.assertEqual([(a['name'], a['status'], a['granted']) for a in caps['adapters']], [('echo', 'ready', ['observe'])])
        self.assertNotIn('script', caps['adapters'][0])
        by_id = {c['id']: c for c in caps['capabilities']}
        self.assertTrue(by_id['adapter.observe']['available'])
        self.assertTrue(by_id['adapter.submit']['requires_approval'])
        mode = self.c.request('GET', '/api/studio/mode')[1]
        self.assertEqual(mode['executors']['adapter']['status'], 'ready')
        self.assertFalse(mode['demo_only'])          # a granted, non-DEMO adapter exists: say so

    def test_end_to_end_adapter_run_over_http(self):
        adapter = self.enroll()[1]
        self.c.request('POST', f'/api/studio/adapters/{adapter["id"]}/grant',
                       {'scopes': ['observe', 'submit'], 'confirm': adapter['id']})
        status, run = self.c.request('POST', '/api/studio/runs',
                                     {'name': 'adapter e2e', 'spec': support.adapter_spec('submit:e2e', outputs=('out',))},
                                     headers={'Idempotency-Key': 'ahs04-e2e-1'})
        self.assertEqual(status, 201, run)
        self.assertEqual((run['demo'], run['label']), (False, None))
        done = support.wait_for(lambda: (lambda r: r if r['status'] in ('succeeded', 'failed', 'blocked') else None)(
            self.c.request('GET', f'/api/studio/runs/{run["id"]}')[1]), timeout=20)
        self.assertEqual(done['status'], 'succeeded', done)
        events = self.c.request('GET', f'/api/studio/runs/{run["id"]}/events?limit=200')[1]['events']
        self.assertEqual(adapter_protocol.verify_stored(events)['events'], 5)
        evidence = self.c.request('GET', f'/api/studio/runs/{run["id"]}/evidence')[1]
        self.assertEqual((evidence['sealed'], evidence['demo'], len(evidence['artifacts'])), (True, False, 1))

    def test_revoke_is_confirmed_one_way_and_frees_the_name(self):
        adapter = self.enroll()[1]
        path = f'/api/studio/adapters/{adapter["id"]}/revoke'
        self.assertEqual(self.c.request('POST', path, {})[0], 400)
        status, revoked = self.c.request('POST', path, {'confirm': adapter['id']})
        self.assertEqual((status, revoked['revoked'], revoked['granted'], revoked['sessions_stopped']), (200, True, [], 0))
        self.assertEqual(self.c.request('POST', path, {'confirm': adapter['id']})[0], 409)
        self.assertEqual(self.c.request('POST', f'/api/studio/adapters/{adapter["id"]}/grant',
                                        {'scopes': ['observe'], 'confirm': adapter['id']})[0], 409)
        self.assertEqual(self.enroll()[0], 201)                               # re-enrollment is a new record
        listed = self.c.request('GET', '/api/studio/adapters')[1]['adapters']
        self.assertEqual([a['revoked'] for a in listed], [True, False])

    def test_enrollment_rejects_unsafe_scripts_and_secret_args(self):
        data = self.app.mode.data_dir
        inside = data / 'evil'
        inside.mkdir(mode=0o700)
        shutil.copyfile(self.echo, inside / 'a.py')
        cases = [({'script': 'relative.py'}, 'schema_invalid'), ({'script': str(self.dir / 'missing.py')}, 'schema_invalid'),
                 ({'script': str(inside / 'a.py')}, 'schema_invalid'), ({'script': str(self.dir)}, 'schema_invalid'),
                 ({'name': 'Bad Name'}, 'schema_invalid'), ({'shell': True}, 'unknown_field'),
                 ({'args': ['--token=abc']}, 'adapter_secret_detected'),
                 ({'args': ['--key', 'x']}, None), ({'args': ['sk-' + 'a' * 30]}, 'adapter_secret_detected'),
                 ({'args': ['a\nb']}, 'schema_invalid'), ({'args': ['x'] * 17}, 'schema_invalid')]
        for i, (body, code) in enumerate(cases):
            with self.subTest(body=body):
                status, result = self.enroll(**dict({'name': f'a{i}'}, **body))
                if code is None:
                    self.assertEqual(status, 201, result)
                else:
                    self.assertEqual(result.get('error'), code, result)
                    self.assertNotIn('abc', json.dumps(result))

    def test_group_writable_script_refused(self):
        os.chmod(self.echo, 0o620)
        self.assertEqual(self.enroll()[1]['error'], 'schema_invalid')

    def test_blueprint_export_never_contains_enrollment(self):
        adapter = self.enroll(args=['--profile', 'local'])[1]
        spec = support.adapter_spec()
        status, created = self.c.request('POST', '/api/studio/definitions', {'name': 'adapter bp', 'spec': spec})
        self.assertEqual(status, 201, created)
        status, bundle = self.c.request('POST', '/api/studio/export', {})
        self.assertEqual(status, 200)
        text = json.dumps(bundle)
        for leaked in (adapter['id'], self.echo, str(self.dir), '--profile', adapter['script_sha256']):
            self.assertNotIn(leaked, text)
        self.assertIn('"ref": "echo"', text)    # the blueprint refers to the adapter by enrollment name only


class AdapterSpecValidationTests(unittest.TestCase):
    def test_adapter_specs(self):
        schema.validate_spec(support.adapter_spec('submit:task'))
        schema.validate_spec(support.adapter_spec('observe:job-1'))
        bad = [('observe:has space', 'schema_invalid'), ('mock:success', 'schema_invalid'), ('shell:id', 'schema_invalid')]
        for step, code in bad:
            spec = support.adapter_spec('submit:x')
            spec['stages'][0]['executor_step'] = step
            with self.subTest(step=step), self.assertRaises(SchemaError) as caught:
                schema.validate_spec(spec)
            self.assertEqual(caught.exception.code, code)
        spec = support.adapter_spec('submit:x')
        spec['stages'][0]['capabilities'] = ['adapter.observe']
        with self.assertRaises(SchemaError) as caught:
            schema.validate_spec(spec)
        self.assertEqual(caught.exception.code, 'capability_not_allowed')
        spec = support.adapter_spec()
        spec['capabilities'].remove('adapter.observe')
        with self.assertRaises(SchemaError):
            schema.validate_spec(spec)
        spec = support.adapter_spec(ref='not a key')
        with self.assertRaises(SchemaError):
            schema.validate_spec(spec)


class EnrollmentStoreTests(unittest.TestCase):
    def setUp(self):
        self.s, self.ex, self.data, self.dir = support.adapter_env(self)
        self.adapter = support.enroll(self.s, self.data, self.dir / 'echo_adapter.py', scopes=())

    def test_launch_fields_immutable_and_rows_never_deleted(self):
        db = sqlite3.connect(self.s.path, isolation_level=None)
        self.addCleanup(db.close)
        for sql in ("UPDATE adapters SET script='/bin/sh'", "UPDATE adapters SET args_json='[\"-c\"]'",
                    "UPDATE adapters SET script_sha256='x'", "DELETE FROM adapters"):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                db.execute(sql)
        self.s.revoke_adapter(self.adapter['id'])
        with self.assertRaises(sqlite3.DatabaseError):
            db.execute("UPDATE adapters SET revoked_utc=NULL")

    def test_validate_enrollment_returns_pinned_hash(self):
        item = validate_enrollment({'name': 'x', 'script': str(self.dir / 'echo_adapter.py')}, self.data)
        self.assertEqual(item['sha256'], schema.sha256_hex((self.dir / 'echo_adapter.py').read_bytes()))
        self.assertEqual(self.adapter['script_sha256'], item['sha256'])

    def test_enrollment_limit(self):
        for i in range(15):
            support.enroll(self.s, self.data, self.dir / 'echo_adapter.py', name=f'n{i}', scopes=())
        with self.assertRaises(Exception):
            support.enroll(self.s, self.data, self.dir / 'echo_adapter.py', name='n99', scopes=())


if __name__ == '__main__':
    unittest.main()
