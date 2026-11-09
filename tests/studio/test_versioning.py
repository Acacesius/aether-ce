"""AHS-02.4-02.6: versioning (CAS), immutable run snapshots + overrides, v1 -> v2 migration."""
import copy
import json
import sqlite3
import threading
import unittest

from aether.studio import schema
from aether.studio.store import CasConflict, TerminalState

from tests.studio import support


def spec_with_input(mission='DEMO: v1', **kw):
    spec = schema.demo_spec(**kw)
    spec['mission'] = mission
    spec['inputs'] = [{'name': 'topic', 'type': 'string', 'default': 'alpha'}]
    return schema.validate_spec(spec)


class VersioningTests(unittest.TestCase):
    def setUp(self):
        self.s = support.store(self)

    def create(self, mission='DEMO: v1'):
        return self.s.create_definition('lib', 'desc', spec_with_input(mission))

    def test_update_creates_new_version(self):
        d = self.create()
        self.assertEqual((d['rev'], d['latest_version']), (0, 1))
        d2 = self.s.update_definition(d['id'], 0, spec=spec_with_input('DEMO: v2'))
        self.assertEqual((d2['rev'], d2['latest_version']), (1, 2))
        same = self.s.update_definition(d['id'], 1, spec=spec_with_input('DEMO: v2'), description='new text')
        self.assertEqual(same['latest_version'], 2, 'identical spec must not add a version')
        self.assertEqual(same['description'], 'new text')
        versions = self.s.get_definition(d['id'])['versions']
        self.assertEqual([v['version'] for v in versions], [1, 2])
        self.assertNotEqual(versions[0]['spec_sha256'], versions[1]['spec_sha256'])
        with self.assertRaises(CasConflict):
            self.s.update_definition(d['id'], 0, name='stale')

    def test_version_rows_immutable(self):
        d = self.create()
        vid = self.s.get_definition(d['id'])['versions'][0]['id']
        for sql in ("UPDATE harness_versions SET spec_json='{}' WHERE id=?", 'DELETE FROM harness_versions WHERE id=?'):
            with self.assertRaises(sqlite3.DatabaseError):
                self.s._db.execute(sql, (vid,))
        self.s.update_definition(d['id'], 0, spec=spec_with_input('DEMO: v2'))
        self.assertEqual(self.s.get_version(vid)['spec']['mission'], 'DEMO: v1')

    def test_archived_definition_cannot_be_updated(self):
        d = self.create()
        self.s.set_archived(d['id'], True)
        with self.assertRaises(TerminalState):
            self.s.update_definition(d['id'], 1, name='x')

    def test_concurrent_updates_one_wins(self):
        d = self.create()
        results = []
        barrier = threading.Barrier(8)

        def update(i):
            barrier.wait()
            try:
                self.s.update_definition(d['id'], 0, spec=spec_with_input(f'DEMO: racer {i}'))
                results.append('ok')
            except CasConflict:
                results.append('conflict')
        threads = [threading.Thread(target=update, args=(i,)) for i in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(results), ['conflict'] * 7 + ['ok'])
        final = self.s.get_definition(d['id'])
        self.assertEqual((final['rev'], final['latest_version']), (1, 2))
        self.assertEqual(len(final['versions']), 2)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.s = support.store(self)

    def submit(self, def_id, overrides=None, key=None):
        vid = self.s.latest_version_id(def_id)
        spec = schema.validate_spec(self.s.get_version(vid)['spec'])
        snap = schema.apply_overrides(spec, overrides or {})
        run_id, _ = self.s.submit(vid, snap, overrides or {}, idempotency_key=key or schema.new_id('run'),
                                  request_sha256=schema.sha256_hex('x'))
        return run_id

    def test_run_snapshot_frozen_after_definition_edit(self):
        d = self.s.create_definition('lib', '', spec_with_input('DEMO: v1'))
        run_id = self.submit(d['id'], {'topic': 'beta'})
        before = self.s.run_row(run_id)
        self.s.update_definition(d['id'], 0, spec=spec_with_input('DEMO: v2 changed'))
        after = self.s.run_row(run_id)
        for column in ('snapshot_json', 'snapshot_sha256', 'overrides_json', 'version_id'):
            self.assertEqual(before[column], after[column])
        self.assertEqual(json.loads(after['snapshot_json'])['mission'], 'DEMO: v1')
        self.assertEqual(json.loads(after['snapshot_json'])['inputs'][0]['default'], 'beta')
        with self.assertRaises(sqlite3.DatabaseError):
            self.s._db.execute("UPDATE runs SET snapshot_json='{}' WHERE id=?", (run_id,))
        newer = self.submit(d['id'])
        self.assertIn('v2 changed', json.loads(self.s.run_row(newer)['snapshot_json'])['mission'])

    def test_override_only_declared_inputs(self):
        spec = spec_with_input()
        self.assertEqual(schema.apply_overrides(spec, {'topic': 'gamma'})['inputs'][0]['default'], 'gamma')
        self.assertEqual(spec['inputs'][0]['default'], 'alpha', 'definition spec is never mutated')
        for bad in ({'mission': 'x'}, {'executor': {}}, {'capabilities': ['workspace.write']}, {'topic': 5},
                    {'limits': {'wall_seconds': 10**6}}, {'limits': {'tokens': 1}}):
            with self.assertRaises(schema.SchemaError, msg=bad) as ctx:
                schema.apply_overrides(spec, bad)
            self.assertIn(ctx.exception.code, ('override_not_allowed', 'schema_invalid'))

    def test_old_run_reproducible(self):
        d = self.s.create_definition('lib', '', spec_with_input('DEMO: v1'))
        run_id = self.submit(d['id'], {'topic': 'beta'})
        original = self.s.run_row(run_id)
        stored = json.loads(original['snapshot_json'])
        digest = schema.spec_sha256(stored)
        self.assertEqual(digest, original['snapshot_sha256'])
        for i in range(3):
            d = self.s.update_definition(d['id'], i, spec=spec_with_input(f'DEMO: edit {i}'))
        again = schema.validate_spec(json.loads(self.s.run_row(run_id)['snapshot_json']))
        self.assertEqual(schema.spec_sha256(again), digest)
        self.assertEqual(again, stored)


class MigrationTests(unittest.TestCase):
    def test_v1_to_v2_spec_upgrade(self):
        s = support.store(self)
        d = s.create_definition('lib', '', spec_with_input())
        vid = s.latest_version_id(d['id'])
        spec = schema.validate_spec(s.get_version(vid)['spec'])
        run_id, _ = s.submit(vid, schema.apply_overrides(spec, {}), {}, idempotency_key='k1', request_sha256='r')
        original = s.run_row(run_id)['snapshot_json']
        frozen = copy.deepcopy(spec)

        up = schema.upgrade_spec_v1_to_v2(spec)
        self.assertEqual(spec, frozen, 'pure: input untouched')
        self.assertEqual(up['tags'], [])
        self.assertEqual({k: v for k, v in up.items() if k != 'tags'}, spec)
        self.assertEqual(schema.validate_spec_v2(up), up)
        with self.assertRaises(schema.SchemaError):
            schema.validate_spec(up)  # v1 still rejects the new field
        with self.assertRaises(schema.SchemaError):
            schema.upgrade_spec_v1_to_v2(up)
        bad = copy.deepcopy(up)
        bad['tags'] = ['Bad Tag']
        with self.assertRaises(schema.SchemaError):
            schema.validate_spec_v2(bad)

        result = s.upgrade_run_snapshot(run_id)
        self.assertEqual(result['to_schema'], schema.SCHEMA_V2)
        self.assertEqual(s.run_row(run_id)['snapshot_json'], original, 'original bytes preserved verbatim')
        self.assertEqual(result['original_sha256'], schema.sha256_hex(original))
        self.assertNotEqual(result['upgraded_sha256'], result['original_sha256'])
        self.assertEqual(s.upgrade_run_snapshot(run_id), result, 'idempotent')
        with self.assertRaises(sqlite3.DatabaseError):
            s._db.execute("UPDATE run_snapshot_upgrades SET upgraded_json='{}' WHERE run_id=?", (run_id,))


if __name__ == '__main__':
    unittest.main()
