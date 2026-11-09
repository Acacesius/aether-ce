import sqlite3
import unittest

from aether.studio import schema
from aether.studio.store import CasConflict, IdempotencyConflict, NotFound, QueueFull, Store, TerminalState, events_sha256

from tests.studio import support


class StoreTests(unittest.TestCase):
    def raw(self, s):
        return s._db

    def test_migrate_idempotent(self):
        s = support.store(self)
        self.assertEqual(s.migrate(), [])
        s.close()
        s2 = Store(s.path)
        self.addCleanup(s2.close)
        self.assertEqual(s2.migrate(), [])
        self.assertEqual([r[0] for r in s2._db.execute('SELECT version FROM schema_migrations')], [1, 2, 3, 4, 5])
        self.assertEqual(s2._db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        self.assertEqual(s2._db.execute('PRAGMA foreign_keys').fetchone()[0], 1)

    def test_versions_immutable_trigger(self):
        s = support.store(self)
        run_id = support.submit(s)
        version = s.run_row(run_id)['version_id']
        for sql in ("UPDATE harness_versions SET spec_json='{}' WHERE id=?", 'DELETE FROM harness_versions WHERE id=?'):
            with self.assertRaisesRegex(sqlite3.DatabaseError, 'immutable'):
                self.raw(s).execute(sql, (version,))

    def test_run_snapshot_immutable_trigger(self):
        s = support.store(self)
        run_id = support.submit(s)
        for col in ('snapshot_json', 'snapshot_sha256', 'overrides_json'):
            with self.assertRaisesRegex(sqlite3.DatabaseError, 'immutable'):
                self.raw(s).execute(f"UPDATE runs SET {col}='x' WHERE id=?", (run_id,))
        with self.assertRaisesRegex(sqlite3.DatabaseError, 'immutable'):
            self.raw(s).execute('DELETE FROM runs WHERE id=?', (run_id,))

    def test_events_append_only(self):
        s = support.store(self)
        run_id = support.submit(s)
        with self.assertRaisesRegex(sqlite3.DatabaseError, 'append_only'):
            self.raw(s).execute("UPDATE run_events SET type='x' WHERE run_id=?", (run_id,))
        with self.assertRaisesRegex(sqlite3.DatabaseError, 'append_only'):
            self.raw(s).execute('DELETE FROM run_events WHERE run_id=?', (run_id,))
        with self.assertRaisesRegex(sqlite3.DatabaseError, 'seq_gap'):
            self.raw(s).execute("INSERT INTO run_events(run_id, seq, utc, type, demo, data_json) VALUES (?,?,?,?,?,?)",
                                (run_id, 9, 'x', 'run.status', 1, '{}'))

    def test_events_are_gapless(self):
        s = support.store(self)
        run_id = support.submit(s)
        for i in range(5):
            s.append_event(run_id, 'executor.error', {'code': str(i), 'message': ''})
        self.assertEqual([e['seq'] for e in s.events(run_id)[0]], [1, 2, 3, 4, 5, 6])

    def test_cas_conflict_raises(self):
        s = support.store(self)
        run_id = support.submit(s)
        rev = s.run_row(run_id)['rev']
        s.set_status(run_id, rev=rev, to='blocked', reason='approval_pending')
        with self.assertRaises(CasConflict):
            s.set_status(run_id, rev=rev, to='queued')

    def test_terminal_state_is_final(self):
        s = support.store(self)
        run_id = support.submit(s)
        self.assertEqual(s.request_cancel(run_id), 'cancelled')
        with self.assertRaises(TerminalState):
            s.request_cancel(run_id)
        with self.assertRaises(TerminalState):
            s.set_status(run_id, rev=s.run_row(run_id)['rev'], to='queued')
        with self.assertRaisesRegex(sqlite3.DatabaseError, 'terminal_state'):
            self.raw(s).execute("UPDATE runs SET status='queued' WHERE id=?", (run_id,))

    def test_idempotent_submit_and_conflict(self):
        s = support.store(self)
        spec = schema.validate_spec(schema.demo_spec())
        vid = s.ensure_inline_version('x', spec)
        a = s.submit(vid, spec, {}, idempotency_key='k1', request_sha256='aa')
        b = s.submit(vid, spec, {}, idempotency_key='k1', request_sha256='aa')
        self.assertEqual((a[1], b[1], a[0] == b[0]), (True, False, True))
        with self.assertRaises(IdempotencyConflict):
            s.submit(vid, spec, {}, idempotency_key='k1', request_sha256='bb')
        self.assertEqual(len(s.list_runs()), 1)

    def test_inline_version_dedupe_and_bump(self):
        s = support.store(self)
        v1 = s.ensure_inline_version('n', schema.validate_spec(schema.demo_spec()))
        self.assertEqual(v1, s.ensure_inline_version('n', schema.validate_spec(schema.demo_spec())))
        v2 = s.ensure_inline_version('n', schema.validate_spec(schema.demo_spec('mock:fail:2')))
        self.assertNotEqual(v1, v2)
        self.assertEqual((s.get_version(v1)['version'], s.get_version(v2)['version']), (1, 2))
        with self.assertRaises(NotFound):
            s.get_version('hv_' + '0' * 26)

    def test_queue_limit(self):
        s = support.store(self)
        for i in range(100):
            support.submit(s, key=f'k{i}')
        with self.assertRaises(QueueFull):
            support.submit(s, key='overflow')

    def test_snapshot_stored_canonical_and_hash_reproducible(self):
        s = support.store(self)
        run_id = support.submit(s, overrides={'limits': {'wall_seconds': 30}})
        run = s.get_run(run_id)
        self.assertEqual(run['snapshot']['limits']['wall_seconds'], 30)
        self.assertEqual(run['overrides'], {'limits': {'wall_seconds': 30}})
        self.assertEqual(schema.spec_sha256(schema.validate_spec(run['snapshot'])), run['snapshot_sha256'])
        self.assertTrue(run['demo'])
        self.assertEqual(run['label'], 'DEMO')

    def test_run_snapshot_frozen_after_definition_edit(self):
        s = support.store(self)
        run_id = support.submit(s, name='same')
        before = s.get_run(run_id)['snapshot_sha256']
        support.submit(s, 'mock:fail:3', name='same', key='k2')
        self.assertEqual(s.get_run(run_id)['snapshot_sha256'], before)

    def test_events_sha_helper_is_canonical(self):
        self.assertEqual(events_sha256([{'b': 1, 'a': 2}]), events_sha256([{'a': 2, 'b': 1}]))
