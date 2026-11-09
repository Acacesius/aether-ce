"""AHS-02.8: export/import without secrets, scanner rules, artifact secret-shape scanning."""
import copy
import json
import unittest

from aether.studio import exportimport, scanner, schema
from aether.studio.schema import SchemaError

from tests.studio import support


def vendor_key():
    return 'sk-' + 'A1b2' * 6          # built at runtime: no secret-shaped literal in the source


def spec(mission='DEMO: exported'):
    s = schema.demo_spec()
    s['mission'] = mission
    s['inputs'] = [{'name': 'topic', 'type': 'string', 'default': 'alpha'}]
    return schema.validate_spec(s)


def rehash(bundle):
    bundle['sha256'] = exportimport.bundle_hash(bundle['definitions'])
    return bundle


class ExportImportTests(unittest.TestCase):
    def setUp(self):
        self.s = support.store(self)
        self.d = self.s.create_definition('lib', 'a library entry', spec())
        self.s.update_definition(self.d['id'], 0, spec=spec('DEMO: exported v2'))

    def bundle(self):
        return exportimport.export_bundle(self.s)

    def fresh(self):
        return support.store(self)

    def test_export_has_no_secret_fields(self):
        bundle = self.bundle()
        text = json.dumps(bundle)
        self.assertNotIn('"secret"', text, 'declared-input secret flag is stripped')
        self.assertEqual(scanner.find_in_value(bundle), None)
        for banned in ('"id"', 'hd_', 'hv_', 'run_', 'rev', 'created_utc', 'owner', 'token'):
            self.assertNotIn(banned, text)
        self.assertEqual([len(d['versions']) for d in bundle['definitions']], [2])
        self.assertEqual(scanner.strip_secret_fields({'a': 1, 'api_token': 'x', 'n': [{'Password': 'p', 'k': 2}]}),
                         {'a': 1, 'n': [{'k': 2}]})
        self.assertEqual(bundle['definitions'][0]['versions'][0]['spec']['workspace'].get('mount'), None)

    def test_import_roundtrip_hash_equal(self):
        bundle = self.bundle()
        other = self.fresh()
        result = exportimport.import_bundle(other, bundle)
        self.assertEqual(len(result['created']), 1)
        created = result['created'][0]
        self.assertNotEqual(created['id'], self.d['id'])
        self.assertFalse(created['archived'])
        self.assertEqual(created['latest_version'], 2)
        again = exportimport.export_bundle(other)
        self.assertEqual(again['sha256'], bundle['sha256'])
        self.assertEqual(again['definitions'], bundle['definitions'])
        # imported specs hash identically to the originals
        orig = [v['spec_sha256'] for v in self.s.definition_versions(self.d['id'])]
        new = [v['spec_sha256'] for v in other.definition_versions(created['id'])]
        self.assertEqual(orig, new)

    def test_import_is_idempotent_by_name_and_hash(self):
        bundle = self.bundle()
        first = exportimport.import_bundle(self.s, bundle)
        self.assertEqual((len(first['created']), len(first['skipped'])), (0, 1))
        other = self.fresh()
        exportimport.import_bundle(other, bundle)
        second = exportimport.import_bundle(other, bundle)
        self.assertEqual((len(second['created']), len(second['skipped'])), (0, 1))
        self.assertEqual(len(other.list_definitions()), 1)

    def test_import_rejects_secret_like_values(self):
        for label, value in (('vendor', vendor_key()),
                             ('pem', '-----BEGIN ' + 'RSA PRIVATE KEY-----'),
                             ('bearer', 'Bearer ' + 'a' * 30),
                             ('token43', 'A' * 43),
                             ('hex', 'token=' + 'ab' * 20)):
            bundle = copy.deepcopy(self.bundle())
            bundle['definitions'][0]['versions'][0]['spec']['mission'] = 'DEMO ' + value
            other = self.fresh()
            with self.assertRaises(SchemaError, msg=label) as ctx:
                exportimport.import_bundle(other, rehash(bundle))
            self.assertEqual(ctx.exception.code, 'import_secret_detected')
            self.assertNotIn(value, str(ctx.exception.as_dict()), 'matched text is never echoed')
            self.assertEqual(other.list_definitions(), [])

    def test_import_rejects_secret_named_fields_with_values(self):
        bundle = copy.deepcopy(self.bundle())
        bundle['definitions'][0]['api_token'] = 'x'
        with self.assertRaises(SchemaError) as ctx:
            exportimport.import_bundle(self.fresh(), rehash(bundle))
        self.assertEqual(ctx.exception.code, 'import_secret_detected')
        bundle = copy.deepcopy(self.bundle())
        bundle['definitions'][0]['versions'][0]['spec']['inputs'][0]['secret'] = True
        with self.assertRaises(SchemaError) as ctx:
            exportimport.import_bundle(self.fresh(), rehash(bundle))
        self.assertEqual(ctx.exception.code, 'import_secret_detected')

    def test_import_unapproved_capability_rejected(self):
        for caps in (['demo.run', 'shell.exec'], ['demo.run', 'net.egress.allowlist'], ['demo.run', 'provider.cloud']):
            bundle = copy.deepcopy(self.bundle())
            version = bundle['definitions'][0]['versions'][0]
            version['spec']['capabilities'] = caps
            version['spec_sha256'] = schema.spec_sha256(version['spec'])
            other = self.fresh()
            with self.assertRaises(SchemaError, msg=caps) as ctx:
                exportimport.import_bundle(other, rehash(bundle))
            self.assertEqual(ctx.exception.code, 'capability_not_allowed')
            self.assertEqual(other.list_definitions(), [])

    def test_import_rejects_unknown_fields_and_bad_hashes(self):
        cases = []
        b = copy.deepcopy(self.bundle()); b['extra'] = 1; cases.append(('unknown_field', b))
        b = copy.deepcopy(self.bundle()); b['definitions'][0]['owner'] = 'x'; cases.append(('unknown_field', rehash(b)))
        b = copy.deepcopy(self.bundle()); b['definitions'][0]['versions'][0]['spec']['bogus'] = 1
        cases.append(('unknown_field', rehash(b)))
        b = copy.deepcopy(self.bundle()); b['sha256'] = '0' * 64; cases.append(('schema_invalid', b))
        b = copy.deepcopy(self.bundle()); b['definitions'][0]['versions'][0]['spec']['mission'] = 'DEMO: tampered'
        cases.append(('schema_invalid', rehash(b)))  # spec_sha256 no longer matches
        b = copy.deepcopy(self.bundle()); b['format'] = 'other/1'; cases.append(('schema_invalid', b))
        b = copy.deepcopy(self.bundle()); b['definitions'] = []; cases.append(('schema_invalid', rehash(b)))
        for code, bundle in cases:
            other = self.fresh()
            with self.assertRaises(SchemaError) as ctx:
                exportimport.import_bundle(other, bundle)
            self.assertEqual(ctx.exception.code, code)
            self.assertEqual(other.list_definitions(), [])

    def test_import_all_or_nothing(self):
        bundle = copy.deepcopy(self.bundle())
        second = copy.deepcopy(bundle['definitions'][0])
        second['name'] = 'bad'
        second['versions'] = second['versions'][:1]
        second['versions'][0]['spec']['capabilities'] = ['shell.exec']
        bundle['definitions'].append(second)
        other = self.fresh()
        with self.assertRaises(SchemaError):
            exportimport.import_bundle(other, rehash(bundle))
        self.assertEqual(other.list_definitions(), [])


class ExportImportApiTests(unittest.TestCase):
    def test_api_flow(self):
        app, c = support.serve(self)
        s = spec()
        status, d = c.request('POST', '/api/studio/definitions', {'name': 'lib', 'spec': s})
        self.assertEqual(status, 201)
        status, bundle = c.request('POST', '/api/studio/export', {})
        self.assertEqual(status, 200)
        self.assertEqual(c.request('POST', '/api/studio/export', {'definition_ids': [d['id']]})[1]['sha256'], bundle['sha256'])
        self.assertEqual(c.request('POST', '/api/studio/export', {'definition_ids': ['nope']})[0], 400)
        status, res = c.request('POST', '/api/studio/import', bundle)
        self.assertEqual((status, len(res['skipped'])), (201, 1))
        bundle['definitions'][0]['name'] = 'renamed'
        rehash(bundle)
        status, res = c.request('POST', '/api/studio/import', bundle)
        self.assertEqual((status, len(res['created'])), (201, 1))
        bundle['definitions'][0]['versions'][0]['spec']['mission'] = 'DEMO ' + vendor_key()
        status, err = c.request('POST', '/api/studio/import', rehash(bundle))
        self.assertEqual((status, err['error']), (400, 'import_secret_detected'))
        self.assertNotIn(vendor_key(), json.dumps(err))


class ScannerTests(unittest.TestCase):
    def test_rules_and_redaction_never_return_text(self):
        text = f'a {vendor_key()} b Bearer {"x" * 24} c'
        findings = scanner.scan(text)
        self.assertEqual({f['rule'] for f in findings}, {'vendor_key_shape', 'bearer_literal'})
        self.assertTrue(all(set(f) == {'rule', 'start', 'end'} for f in findings))
        clean, rules = scanner.redact(text)
        self.assertNotIn(vendor_key(), clean)
        self.assertIn('[REDACTED:vendor_key_shape]', clean)
        self.assertEqual(rules, ['bearer_literal', 'vendor_key_shape'])
        self.assertEqual(scanner.scan('plain DEMO text, sha ' + 'ab' * 32), [])
        self.assertEqual(scanner.redact('nothing here'), ('nothing here', []))

    def test_artifact_secret_shapes_redacted_and_flagged(self):
        s = support.store(self)
        secret = vendor_key()
        # drive the runner's artifact path with secret-shaped content via a custom mock step
        from aether.studio.executors.mock import MockExecutor

        class Leaky(MockExecutor):
            def start(self_inner, run, stage, emit):
                emit('artifact.write', {'name': stage.outputs[0], 'media_type': 'text/plain',
                                        'content': f'key material {secret} end\n'})
                emit('artifact.write', {'name': stage.outputs[0], 'media_type': 'image/png',
                                        'content': f'\x89PNG {secret}'})
                return super().start(run, stage, emit)
        support.runner(self, s, executor=Leaky())
        run_id = support.submit(s, 'mock:success', outputs=('report',))
        run = support.wait_status(s, run_id, 'succeeded', 'failed')
        self.assertEqual(len(run['artifacts']), 1, 'secret-bearing png is refused, text is redacted')
        art = run['artifacts'][0]
        self.assertTrue(art['flagged'])
        events = s.events(run_id)[0]
        self.assertIn('artifact.flagged', [e['type'] for e in events])
        self.assertIn('artifact_secret_detected', json.dumps(events))
        self.assertNotIn(secret, json.dumps(events) + json.dumps(run))
        # artifact sha is over the redacted bytes
        clean, _ = scanner.redact(f'key material {secret} end\n')
        self.assertEqual(art['sha256'], schema.sha256_hex(clean))

    def test_clean_artifact_not_flagged(self):
        s = support.store(self)
        support.runner(self, s)
        run_id = support.submit(s, 'mock:artifact:report', outputs=('report',))
        run = support.wait_status(s, run_id, 'succeeded')
        self.assertFalse(run['artifacts'][0]['flagged'])


if __name__ == '__main__':
    unittest.main()
