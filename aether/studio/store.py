"""SQLite (stdlib) repository for the CE Studio: migrations, WAL, BEGIN IMMEDIATE + rev CAS.

All writes go through ``tx()``; events are appended in the same transaction as the state change they describe.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
import time

from aether.studio import schema
from aether.studio.orchestration_store import OrchestrationMixin
from aether.studio.schema import TERMINAL, canonical_json, new_id, sha256_hex, utc

MIGRATIONS = Path(__file__).resolve().parent / 'migrations'
QUEUE_LIMIT = 100
ADAPTER_LIMIT = 16
FK_OFF_MARK = '-- foreign_keys=OFF'


class StoreError(Exception):
    code = 'invalid_request'


class CasConflict(StoreError):
    code = 'rev_conflict'


class TerminalState(StoreError):
    code = 'terminal_state'


class IdempotencyConflict(StoreError):
    code = 'idempotency_conflict'


class QueueFull(StoreError):
    code = 'queue_full'


class NotFound(StoreError, LookupError):
    code = 'not_found'


def events_sha256(events: list[dict]) -> str:
    """sha256 over the canonical JSON array of API event objects in seq order."""
    return sha256_hex(canonical_json(events))


class Store(OrchestrationMixin):
    def __init__(self, path, *, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), timeout=5, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute('PRAGMA journal_mode=WAL')
        self._db.execute('PRAGMA foreign_keys=ON')
        self._db.execute('PRAGMA busy_timeout=5000')
        self._db.execute('PRAGMA synchronous=FULL')
        self.migrate()

    def close(self):
        with self._lock:
            self._db.close()

    def now(self) -> str:
        return utc(self.clock())

    @contextmanager
    def tx(self):
        with self._lock:
            self._db.execute('BEGIN IMMEDIATE')
            try:
                yield self._db
            except BaseException:
                self._db.execute('ROLLBACK')
                raise
            else:
                self._db.execute('COMMIT')

    # ------------------------------------------------------------ migrations
    def migrate(self) -> list[int]:
        """Apply pending migrations. A file marked ``-- foreign_keys=OFF`` rebuilds tables: the pragma is set outside
        the transaction (SQLite ignores it inside) and ``foreign_key_check`` must be clean before it is restored."""
        applied = []
        for path in sorted(MIGRATIONS.glob('[0-9][0-9][0-9]_*.sql')):
            version = int(path.name[:3])
            text = path.read_text(encoding='utf-8')
            rebuild = FK_OFF_MARK in text
            if rebuild:
                self._db.execute('PRAGMA foreign_keys=OFF')
            try:
                with self.tx() as db:
                    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
                    if exists and db.execute('SELECT 1 FROM schema_migrations WHERE version=?', (version,)).fetchone():
                        continue
                    for statement in _statements(text):
                        db.execute(statement)
                    db.execute('INSERT INTO schema_migrations(version, applied_utc) VALUES (?, ?)', (version, self.now()))
                    if rebuild:
                        assert not db.execute('PRAGMA foreign_key_check').fetchall(), f'migration {version} broke foreign keys'
                    applied.append(version)
            finally:
                if rebuild:
                    self._db.execute('PRAGMA foreign_keys=ON')
        return applied

    # ------------------------------------------------------------ events
    def _append(self, db, run_id, type_, data, *, stage_key=None, demo=None):
        if demo is None:
            demo = db.execute('SELECT demo FROM runs WHERE id=?', (run_id,)).fetchone()['demo']
        seq = db.execute('SELECT COALESCE(MAX(seq), 0) + 1 FROM run_events WHERE run_id=?', (run_id,)).fetchone()[0]
        db.execute('INSERT INTO run_events(run_id, seq, utc, type, stage_key, demo, data_json) VALUES (?,?,?,?,?,?,?)',
                   (run_id, seq, self.now(), type_, stage_key, 1 if demo else 0, canonical_json(data)))
        return seq

    def append_event(self, run_id, type_, data, *, stage_key=None, owner=None):
        """Executor/worker output path. With ``owner`` the append happens only while that lease holds."""
        with self.tx() as db:
            if owner is not None and not db.execute(
                    "SELECT 1 FROM runs WHERE id=? AND status='running' AND lease_owner=?", (run_id, owner)).fetchone():
                return None
            return self._append(db, run_id, type_, data, stage_key=stage_key)

    def events(self, run_id, cursor=0, limit=200):
        with self._lock:
            self._require_run(run_id)
            rows = self._db.execute('SELECT * FROM run_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?',
                                    (run_id, cursor, limit + 1)).fetchall()
        items = [_event(r) for r in rows[:limit]]
        return items, len(rows) > limit

    def all_events(self, db, run_id):
        return [_event(r) for r in db.execute('SELECT * FROM run_events WHERE run_id=? ORDER BY seq', (run_id,))]

    # ------------------------------------------------------------ library (minimal; full CRUD is AHS-02.7)
    def ensure_inline_version(self, name: str, spec: dict) -> str:
        sha = schema.spec_sha256(spec)
        with self.tx() as db:
            row = db.execute('SELECT v.id FROM harness_versions v JOIN harness_definitions d ON d.id=v.definition_id '
                             'WHERE d.name=? AND v.spec_sha256=? AND d.archived=0 ORDER BY v.version DESC LIMIT 1',
                             (name, sha)).fetchone()
            if row:
                return row['id']
            now = self.now()
            definition = db.execute('SELECT id, latest_version FROM harness_definitions WHERE name=? AND archived=0',
                                    (name,)).fetchone()
            if definition is None:
                def_id = new_id('hd', now=self.clock())
                db.execute('INSERT INTO harness_definitions(id, name, created_utc, updated_utc, rev, latest_version) '
                           'VALUES (?,?,?,?,0,0)', (def_id, name, now, now))
                latest = 0
            else:
                def_id, latest = definition['id'], definition['latest_version']
            version_id = new_id('hv', now=self.clock())
            db.execute('INSERT INTO harness_versions(id, definition_id, version, schema_version, spec_json, spec_sha256, created_utc) '
                       'VALUES (?,?,?,?,?,?,?)', (version_id, def_id, latest + 1, schema.SCHEMA_VERSION,
                                                  canonical_json(spec), sha, now))
            db.execute('UPDATE harness_definitions SET latest_version=?, updated_utc=?, rev=rev+1 WHERE id=?',
                       (latest + 1, now, def_id))
            return version_id

    def get_version(self, version_id):
        with self._lock:
            row = self._db.execute('SELECT * FROM harness_versions WHERE id=?', (version_id,)).fetchone()
        if row is None:
            raise NotFound(version_id)
        return {'id': row['id'], 'definition_id': row['definition_id'], 'version': row['version'],
                'schema_version': row['schema_version'], 'created_utc': row['created_utc'],
                'spec_sha256': row['spec_sha256'], 'spec': json.loads(row['spec_json'])}

    # ------------------------------------------------------------ library (02.4, 02.7)
    def _definition(self, row, *, versions=None):
        result = {'id': row['id'], 'schema_version': schema.SCHEMA_VERSION, 'name': row['name'],
                  'description': row['description'], 'created_utc': row['created_utc'],
                  'updated_utc': row['updated_utc'], 'archived': bool(row['archived']), 'rev': row['rev'],
                  'latest_version': row['latest_version']}
        if versions is not None:
            result['versions'] = versions
        return result

    def create_definition(self, name, description, spec):
        """Create a definition with version 1. ``spec`` must already be validated."""
        with self.tx() as db:
            def_id, _ = self._insert_definition(db, name, description, [spec])
            return self._definition(db.execute('SELECT * FROM harness_definitions WHERE id=?', (def_id,)).fetchone())

    def _insert_definition(self, db, name, description, specs):
        now = self.now()
        def_id = new_id('hd', now=self.clock())
        db.execute('INSERT INTO harness_definitions(id, name, description, created_utc, updated_utc, rev, latest_version) '
                   'VALUES (?,?,?,?,?,0,?)', (def_id, name, description, now, now, len(specs)))
        version_ids = []
        for number, spec in enumerate(specs, 1):
            vid = new_id('hv', now=self.clock())
            db.execute('INSERT INTO harness_versions(id, definition_id, version, schema_version, spec_json, '
                       'spec_sha256, created_utc) VALUES (?,?,?,?,?,?,?)',
                       (vid, def_id, number, schema.SCHEMA_VERSION, canonical_json(spec), schema.spec_sha256(spec), now))
            version_ids.append(vid)
        return def_id, version_ids

    def update_definition(self, def_id, expected_rev, *, name=None, description=None, spec=None):
        """CAS on ``rev``. A changed spec appends an immutable version; identical spec is metadata-only."""
        with self.tx() as db:
            row = db.execute('SELECT * FROM harness_definitions WHERE id=?', (def_id,)).fetchone()
            if row is None:
                raise NotFound(def_id)
            if row['rev'] != expected_rev:
                raise CasConflict(def_id)
            if row['archived']:
                raise TerminalState('definition archived')
            latest = row['latest_version']
            if spec is not None:
                sha = schema.spec_sha256(spec)
                current = db.execute('SELECT spec_sha256 FROM harness_versions WHERE definition_id=? AND version=?',
                                     (def_id, latest)).fetchone()
                if current is None or current['spec_sha256'] != sha:
                    latest += 1
                    db.execute('INSERT INTO harness_versions(id, definition_id, version, schema_version, spec_json, '
                               'spec_sha256, created_utc) VALUES (?,?,?,?,?,?,?)',
                               (new_id('hv', now=self.clock()), def_id, latest, schema.SCHEMA_VERSION,
                                canonical_json(spec), sha, self.now()))
            cur = db.execute('UPDATE harness_definitions SET name=?, description=?, latest_version=?, updated_utc=?, '
                             'rev=rev+1 WHERE id=? AND rev=?',
                             (row['name'] if name is None else name,
                              row['description'] if description is None else description,
                              latest, self.now(), def_id, expected_rev))
            if cur.rowcount != 1:
                raise CasConflict(def_id)
            return self._definition(db.execute('SELECT * FROM harness_definitions WHERE id=?', (def_id,)).fetchone())

    def set_archived(self, def_id, archived: bool, expected_rev=None):
        with self.tx() as db:
            row = db.execute('SELECT * FROM harness_definitions WHERE id=?', (def_id,)).fetchone()
            if row is None:
                raise NotFound(def_id)
            if expected_rev is not None and row['rev'] != expected_rev:
                raise CasConflict(def_id)
            if bool(row['archived']) != archived:
                db.execute('UPDATE harness_definitions SET archived=?, updated_utc=?, rev=rev+1 WHERE id=?',
                           (1 if archived else 0, self.now(), def_id))
            return self._definition(db.execute('SELECT * FROM harness_definitions WHERE id=?', (def_id,)).fetchone())

    def list_definitions(self, *, include_archived=False):
        with self._lock:
            sql = 'SELECT * FROM harness_definitions' + ('' if include_archived else ' WHERE archived=0')
            rows = self._db.execute(sql + ' ORDER BY created_utc, rowid').fetchall()
        return [self._definition(r) for r in rows]

    def get_definition(self, def_id):
        with self._lock:
            row = self._db.execute('SELECT * FROM harness_definitions WHERE id=?', (def_id,)).fetchone()
            if row is None:
                raise NotFound(def_id)
            versions = self._db.execute('SELECT * FROM harness_versions WHERE definition_id=? ORDER BY version',
                                        (def_id,)).fetchall()
        return self._definition(row, versions=[{'id': v['id'], 'version': v['version'], 'created_utc': v['created_utc'],
                                                'spec_sha256': v['spec_sha256'], 'schema_version': v['schema_version']}
                                               for v in versions])

    def definition_versions(self, def_id):
        """Full version rows (with specs), oldest first."""
        with self._lock:
            rows = self._db.execute('SELECT id FROM harness_versions WHERE definition_id=? ORDER BY version',
                                    (def_id,)).fetchall()
        return [self.get_version(r['id']) for r in rows]

    def latest_version_id(self, def_id, *, allow_archived=False):
        with self._lock:
            row = self._db.execute('SELECT d.archived, v.id FROM harness_definitions d JOIN harness_versions v '
                                   'ON v.definition_id=d.id AND v.version=d.latest_version WHERE d.id=?',
                                   (def_id,)).fetchone()
        if row is None or (row['archived'] and not allow_archived):
            raise NotFound(def_id)
        return row['id']

    def import_definitions(self, items):
        """items: [(name, description, [spec, ...])]. All-or-nothing. Idempotent by (name, final spec_sha256).
        Returns (created, skipped) lists of definition dicts."""
        created, skipped = [], []
        with self.tx() as db:
            for name, description, specs in items:
                final = schema.spec_sha256(specs[-1])
                dup = db.execute('SELECT d.id FROM harness_definitions d JOIN harness_versions v ON v.definition_id=d.id '
                                 'AND v.version=d.latest_version WHERE d.name=? AND d.archived=0 AND v.spec_sha256=?',
                                 (name, final)).fetchone()
                if dup:
                    skipped.append(self._definition(db.execute('SELECT * FROM harness_definitions WHERE id=?',
                                                               (dup['id'],)).fetchone()))
                    continue
                def_id, _ = self._insert_definition(db, name, description, specs)
                created.append(self._definition(db.execute('SELECT * FROM harness_definitions WHERE id=?',
                                                           (def_id,)).fetchone()))
        return created, skipped

    # ------------------------------------------------------------ migration (02.6)
    def upgrade_run_snapshot(self, run_id):
        """Store a v2 copy of a run's v1 snapshot in ``run_snapshot_upgrades``; the original bytes are untouched."""
        with self.tx() as db:
            row = self._require_run(run_id, db)
            prior = db.execute('SELECT * FROM run_snapshot_upgrades WHERE run_id=?', (run_id,)).fetchone()
            if prior is None:
                upgraded = schema.upgrade_spec_v1_to_v2(json.loads(row['snapshot_json']))
                text = canonical_json(upgraded)
                db.execute('INSERT INTO run_snapshot_upgrades(run_id, from_schema, to_schema, upgraded_json, '
                           'upgraded_sha256, created_utc) VALUES (?,?,?,?,?,?)',
                           (run_id, schema.SCHEMA_VERSION, schema.SCHEMA_V2, text, sha256_hex(text), self.now()))
                prior = db.execute('SELECT * FROM run_snapshot_upgrades WHERE run_id=?', (run_id,)).fetchone()
            return {'run_id': run_id, 'from_schema': prior['from_schema'], 'to_schema': prior['to_schema'],
                    'upgraded': json.loads(prior['upgraded_json']), 'upgraded_sha256': prior['upgraded_sha256'],
                    'original_sha256': row['snapshot_sha256']}

    # ------------------------------------------------------------ adapter enrollments (AHS-04)
    def enroll_adapter(self, name, script, args, script_sha256):
        """New enrollment with no grants. Inputs must already be validated (executors/adapter.validate_enrollment)."""
        with self.tx() as db:
            if db.execute('SELECT COUNT(*) FROM adapters WHERE revoked_utc IS NULL').fetchone()[0] >= ADAPTER_LIMIT:
                raise StoreError(f'at most {ADAPTER_LIMIT} active adapters')
            if db.execute('SELECT 1 FROM adapters WHERE name=? AND revoked_utc IS NULL', (name,)).fetchone():
                raise StoreError('an active adapter with this name exists')
            now = self.now()
            adapter_id = new_id('adp', now=self.clock())
            db.execute('INSERT INTO adapters(id, name, script, args_json, script_sha256, granted_json, created_utc, '
                       "updated_utc) VALUES (?,?,?,?,?,'[]',?,?)",
                       (adapter_id, name, script, canonical_json(list(args)), script_sha256, now, now))
            return _adapter(db.execute('SELECT * FROM adapters WHERE id=?', (adapter_id,)).fetchone())

    def _active_adapter(self, db, adapter_id):
        row = db.execute('SELECT * FROM adapters WHERE id=?', (adapter_id,)).fetchone()
        if row is None:
            raise NotFound(adapter_id)
        if row['revoked_utc'] is not None:
            raise TerminalState('revoked')
        return row

    def grant_adapter(self, adapter_id, scopes):
        """Replace the granted scope set (validated by the caller)."""
        with self.tx() as db:
            self._active_adapter(db, adapter_id)
            db.execute('UPDATE adapters SET granted_json=?, updated_utc=?, rev=rev+1 WHERE id=?',
                       (canonical_json(sorted(scopes)), self.now(), adapter_id))
            return _adapter(db.execute('SELECT * FROM adapters WHERE id=?', (adapter_id,)).fetchone())

    def revoke_adapter(self, adapter_id):
        with self.tx() as db:
            self._active_adapter(db, adapter_id)
            now = self.now()
            db.execute("UPDATE adapters SET revoked_utc=?, granted_json='[]', updated_utc=?, rev=rev+1 WHERE id=?",
                       (now, now, adapter_id))
            return _adapter(db.execute('SELECT * FROM adapters WHERE id=?', (adapter_id,)).fetchone())

    def get_adapter(self, adapter_id):
        with self._lock:
            row = self._db.execute('SELECT * FROM adapters WHERE id=?', (adapter_id,)).fetchone()
        if row is None:
            raise NotFound(adapter_id)
        return _adapter(row)

    def active_adapter_named(self, name):
        """The active enrollment for a blueprint ``executor.ref``, or None."""
        with self._lock:
            row = self._db.execute('SELECT * FROM adapters WHERE name=? AND revoked_utc IS NULL', (name,)).fetchone()
        return _adapter(row) if row else None

    def list_adapters(self, *, include_revoked=False):
        with self._lock:
            sql = 'SELECT * FROM adapters' + ('' if include_revoked else ' WHERE revoked_utc IS NULL')
            rows = self._db.execute(sql + ' ORDER BY created_utc, rowid').fetchall()
        return [_adapter(r) for r in rows]

    # ------------------------------------------------------------ approvals (02.7)
    def list_approvals(self, status=None):
        with self._lock:
            sql = 'SELECT * FROM approvals' + (' WHERE status=?' if status else '') + ' ORDER BY requested_utc, rowid'
            rows = self._db.execute(sql, (status,) if status else ()).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]

    def decide_approval(self, approval_id, decision, confirm):
        """CAS pending -> granted|denied, once. Deny fails a blocked run with approval_denied."""
        with self.tx() as db:
            row = db.execute('SELECT * FROM approvals WHERE id=?', (approval_id,)).fetchone()
            if row is None:
                raise NotFound(approval_id)
            if confirm != row['run_id']:
                raise StoreError('confirm must equal the run id')
            now = self.clock()
            if row['status'] == 'pending' and utc(now) > row['expires_utc']:
                db.execute("UPDATE approvals SET status='expired', decided_utc=? WHERE id=? AND status='pending'",
                           (utc(now), approval_id))
                raise CasConflict('approval expired')
            new = 'granted' if decision == 'grant' else 'denied'
            cur = db.execute("UPDATE approvals SET status=?, decided_utc=?, decided_by='owner' "
                             "WHERE id=? AND status='pending'", (new, utc(now), approval_id))
            if cur.rowcount != 1:
                raise CasConflict('approval already decided')
            run = self._require_run(row['run_id'], db)
            self._append(db, row['run_id'], 'approval.decided',
                         {'approval_id': approval_id, 'capability': row['capability'], 'decision': new},
                         stage_key=row['stage_key'])
            self._audit(db, row['run_id'], 'owner', f'approval.{new}', row['stage_key'], {'capability': row['capability']})
            if new == 'denied' and run['status'] == 'blocked':
                self._finalize(db, run, 'failed', 'approval_denied', None, actor='owner')
            elif run['status'] == 'blocked' and run['reason'] == 'approval_pending' and not db.execute(
                    "SELECT 1 FROM approvals WHERE run_id=? AND status='pending'", (row['run_id'],)).fetchone():
                self._resume(db, run, 'owner', 'approval_granted')
            return dict(db.execute('SELECT * FROM approvals WHERE id=?', (approval_id,)).fetchone())

    # ------------------------------------------------------------ runs
    def submit(self, version_id, snapshot: dict, overrides: dict, *, idempotency_key: str,
               request_sha256: str, relaunch_of=None):
        """Insert a queued run. Returns (run_id, created). Same key + same body replays; else conflict."""
        key_sha = sha256_hex(idempotency_key)
        snap_json = canonical_json(snapshot)
        snap_sha = sha256_hex(snap_json)
        demo = snapshot['executor']['kind'] == 'mock'
        with self.tx() as db:
            prior = db.execute('SELECT request_sha256, run_id FROM submissions WHERE idempotency_key=?', (key_sha,)).fetchone()
            if prior:
                if prior['request_sha256'] != request_sha256:
                    raise IdempotencyConflict('idempotency key reused with a different body')
                return prior['run_id'], False
            if db.execute("SELECT COUNT(*) FROM runs WHERE status='queued'").fetchone()[0] >= QUEUE_LIMIT:
                raise QueueFull('too many queued runs')
            version = db.execute('SELECT definition_id FROM harness_versions WHERE id=?', (version_id,)).fetchone()
            if version is None:
                raise NotFound(version_id)
            run_id = new_id('run', now=self.clock())
            now = self.now()
            db.execute('INSERT INTO runs(id, version_id, definition_id, snapshot_json, snapshot_sha256, overrides_json, '
                       'status, demo, created_utc, relaunch_of) VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (run_id, version_id, version['definition_id'], snap_json, snap_sha, canonical_json(overrides),
                        'queued', 1 if demo else 0, now, relaunch_of))
            for stage in snapshot['stages']:
                db.execute("INSERT INTO stages(id, run_id, key, status, attempt) VALUES (?,?,?,'queued',1)",
                           (new_id('stg', now=self.clock()), run_id, stage['key']))
            db.execute('INSERT INTO submissions(idempotency_key, request_sha256, run_id, created_utc) VALUES (?,?,?,?)',
                       (key_sha, request_sha256, run_id, now))
            self._append(db, run_id, 'run.submitted', {'version_id': version_id, 'snapshot_sha256': snap_sha,
                                                       'idempotency_key_sha256': key_sha}, demo=demo)
            return run_id, True

    def _require_run(self, run_id, db=None):
        row = (db or self._db).execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if row is None:
            raise NotFound(run_id)
        return row

    def run_row(self, run_id):
        with self._lock:
            return dict(self._require_run(run_id))

    def get_run(self, run_id, *, full=True):
        with self._lock:
            row = self._require_run(run_id)
            stages = self._db.execute('SELECT * FROM stages WHERE run_id=? ORDER BY rowid', (run_id,)).fetchall()
            artifacts = self._db.execute('SELECT * FROM artifacts WHERE run_id=? ORDER BY rowid', (run_id,)).fetchall()
        result = _run(row, stages, artifacts, full=full)
        if full:
            result['added_stages'] = [s for item in self.expansions(run_id) for s in item['stages']]
        return result

    def list_runs(self, limit=50, status=None):
        with self._lock:
            if status:
                rows = self._db.execute('SELECT * FROM runs WHERE status=? ORDER BY created_utc DESC, rowid DESC LIMIT ?',
                                        (status, limit)).fetchall()
            else:
                rows = self._db.execute('SELECT * FROM runs ORDER BY created_utc DESC, rowid DESC LIMIT ?', (limit,)).fetchall()
        return [_run(r, (), (), full=False) for r in rows]

    def count(self, status):
        with self._lock:
            return self._db.execute('SELECT COUNT(*) FROM runs WHERE status=?', (status,)).fetchone()[0]

    def set_status(self, run_id, *, rev, to, reason=None):
        """Generic CAS transition (non-terminal). Raises CasConflict on a stale rev."""
        with self.tx() as db:
            row = self._require_run(run_id, db)
            if row['status'] in TERMINAL:
                raise TerminalState(row['status'])
            cur = db.execute('UPDATE runs SET status=?, reason=?, rev=rev+1 WHERE id=? AND rev=?', (to, reason, run_id, rev))
            if cur.rowcount != 1:
                raise CasConflict(run_id)
            self._append(db, run_id, 'run.status', {'from': row['status'], 'to': to, 'reason': reason})

    def claim_next(self, owner, *, lease_seconds, max_running, preflight=None):
        """Serialized FIFO claim. ``preflight(row) -> reason|None`` blocks a run instead of starting it."""
        with self.tx() as db:
            while True:
                if db.execute("SELECT COUNT(*) FROM runs WHERE status='running'").fetchone()[0] >= max_running:
                    return None
                row = db.execute("SELECT * FROM runs WHERE status='queued' AND cancel_requested=0 "
                                 'ORDER BY created_utc, rowid LIMIT 1').fetchone()
                if row is None:
                    return None
                reason = preflight(row) if preflight else None
                if reason:
                    db.execute("UPDATE runs SET status='blocked', reason=?, rev=rev+1 WHERE id=? AND rev=?",
                               (reason, row['id'], row['rev']))
                    self._append(db, row['id'], 'run.status', {'from': 'queued', 'to': 'blocked', 'reason': reason})
                    continue
                now = self.clock()
                cur = db.execute("UPDATE runs SET status='running', started_utc=COALESCE(started_utc, ?), heartbeat_utc=?, lease_owner=?, "
                                 "lease_until=?, rev=rev+1 WHERE id=? AND status='queued' AND rev=?",
                                 (utc(now), utc(now), owner, utc(now + lease_seconds), row['id'], row['rev']))
                if cur.rowcount != 1:
                    raise CasConflict(row['id'])
                self._append(db, row['id'], 'run.status', {'from': 'queued', 'to': 'running', 'reason': None})
                return dict(self._require_run(row['id'], db))

    def heartbeat(self, run_id, owner, *, lease_seconds, emit_event, wall_used=None):
        now = self.clock()
        with self.tx() as db:
            cur = db.execute("UPDATE runs SET heartbeat_utc=?, lease_until=?, wall_used=COALESCE(?, wall_used) "
                             "WHERE id=? AND status='running' AND lease_owner=?",
                             (utc(now), utc(now + lease_seconds), wall_used, run_id, owner))
            if cur.rowcount != 1:
                return False
            if emit_event:
                self._append(db, run_id, 'run.heartbeat', {'lease_until': utc(now + lease_seconds)})
            return True

    def owns(self, run_id, owner):
        with self._lock:
            row = self._db.execute('SELECT status, lease_owner, cancel_requested FROM runs WHERE id=?', (run_id,)).fetchone()
        if row is None or row['status'] != 'running' or row['lease_owner'] != owner:
            return None
        return {'cancel_requested': bool(row['cancel_requested'])}

    def request_cancel(self, run_id):
        """queued/blocked -> cancelled now; running -> flag for the worker; terminal -> TerminalState."""
        with self.tx() as db:
            row = self._require_run(run_id, db)
            if row['status'] in TERMINAL:
                raise TerminalState(row['status'])
            if row['status'] == 'running':
                db.execute('UPDATE runs SET cancel_requested=1, rev=rev+1 WHERE id=?', (run_id,))
                self._audit(db, run_id, 'owner', 'run.cancel_requested', None, {})
                return 'cancel_requested'
            self._finalize(db, row, 'cancelled', 'cancelled_by_owner', None, actor='owner')
            return 'cancelled'

    # ------------------------------------------------------------ stages
    def stages(self, run_id):
        with self._lock:
            return [dict(r) for r in self._db.execute('SELECT * FROM stages WHERE run_id=? ORDER BY rowid', (run_id,))]

    # ------------------------------------------------------------ artifacts
    def add_artifact(self, run_id, owner, stage_key, name, media_type, path, size, digest, flagged=()):
        with self.tx() as db:
            if not db.execute("SELECT 1 FROM runs WHERE id=? AND status='running' AND lease_owner=?", (run_id, owner)).fetchone():
                return None
            row = self._require_run(run_id, db)
            art_id = new_id('art', now=self.clock())
            db.execute('INSERT INTO artifacts(id, run_id, stage_key, name, media_type, size, sha256, path, demo, created_utc, flagged) '
                       'VALUES (?,?,?,?,?,?,?,?,?,?,?)', (art_id, run_id, stage_key, name, media_type, size, digest,
                                                          str(path), row['demo'], self.now(), 1 if flagged else 0))
            if flagged:
                self._append(db, run_id, 'artifact.flagged', {'artifact_id': art_id, 'rules': sorted(flagged)},
                             stage_key=stage_key)
            self._append(db, run_id, 'artifact.created', {'artifact_id': art_id, 'name': name, 'sha256': digest,
                                                          'size': size}, stage_key=stage_key)
            return art_id

    def create_approval(self, run_id, owner, stage_key, capability):
        with self.tx() as db:
            if not db.execute("SELECT 1 FROM runs WHERE id=? AND status='running' AND lease_owner=?", (run_id, owner)).fetchone():
                return None
            apr_id = new_id('apr', now=self.clock())
            now = self.clock()
            db.execute("INSERT INTO approvals(id, run_id, stage_key, capability, status, requested_utc, expires_utc) "
                       "VALUES (?,?,?,?,'pending',?,?)", (apr_id, run_id, stage_key, capability, utc(now), utc(now + 86400)))
            self._append(db, run_id, 'approval.requested', {'approval_id': apr_id, 'capability': capability,
                                                            'decision': None}, stage_key=stage_key)
            return apr_id

    # ------------------------------------------------------------ terminal / recovery
    def finish(self, run_id, owner, to, reason=None, exit_code=None):
        """Worker-side end of a running run (terminal or blocked). False if the lease is no longer ours."""
        with self.tx() as db:
            row = db.execute("SELECT * FROM runs WHERE id=? AND status='running' AND lease_owner=?", (run_id, owner)).fetchone()
            if row is None:
                return False
            self._finalize(db, row, to, reason, exit_code)
            return True

    def _finalize(self, db, row, to, reason, exit_code, actor='runner'):
        run_id = row['id']
        now = self.now()
        if to in TERMINAL:
            leftover_running = 'cancelled' if to == 'cancelled' else 'failed'
            leftover_queued = 'cancelled' if to == 'cancelled' else ('skipped' if to == 'failed' else None)
            for stage in db.execute("SELECT * FROM stages WHERE run_id=? AND status IN ('queued','running','blocked',"
                                    "'interrupted')", (run_id,)).fetchall():
                new = leftover_running if stage['status'] == 'running' else leftover_queued
                if new is None or new == stage['status'] or (stage['status'] == 'interrupted' and to != 'cancelled'):
                    continue
                stage_reason = reason if stage['status'] == 'running' or to == 'cancelled' else 'upstream_failed'
                db.execute('UPDATE stages SET status=?, reason=?, ended_utc=?, rev=rev+1 WHERE id=?',
                           (new, stage_reason, now if new in TERMINAL else None, stage['id']))
                self._append(db, run_id, 'stage.status', {'from': stage['status'], 'to': new, 'attempt': stage['attempt'],
                                                          'reason': stage_reason, 'exit_code': None},
                             stage_key=stage['key'])
        self._append(db, run_id, 'run.status', {'from': row['status'], 'to': to, 'reason': reason})
        self._audit(db, run_id, actor, 'run.finished' if to in TERMINAL else 'run.blocked',
                    None, {'status': to, 'reason': reason, 'exit_code': exit_code})
        if to not in TERMINAL:
            db.execute('UPDATE runs SET status=?, reason=?, lease_owner=NULL, lease_until=NULL, rev=rev+1 WHERE id=?',
                       (to, reason, run_id))
            return
        digest = events_sha256(self.all_events(db, run_id))
        artifacts = [{'id': a['id'], 'name': a['name'], 'sha256': a['sha256'], 'size': a['size'], 'stage_key': a['stage_key']}
                     for a in db.execute('SELECT * FROM artifacts WHERE run_id=? ORDER BY rowid', (run_id,))]
        evidence = {'run_id': run_id, 'snapshot_sha256': row['snapshot_sha256'], 'events_sha256': digest,
                    'artifacts': artifacts, 'demo': bool(row['demo']), 'audit': self._audit_head(db, run_id),
                    'exit': {'status': to, 'reason': reason, 'exit_code': exit_code}}
        db.execute('UPDATE runs SET status=?, reason=?, exit_code=?, ended_utc=?, lease_owner=NULL, lease_until=NULL, '
                   'events_sha256=?, evidence_json=?, rev=rev+1 WHERE id=?',
                   (to, reason, exit_code, now, digest, canonical_json(evidence), run_id))

    def finish_cancelled_queued(self):
        """Cancel flags can only be set on running rows; this is a safety net for crash windows."""
        with self.tx() as db:
            for row in db.execute("SELECT * FROM runs WHERE status IN ('queued','blocked') AND cancel_requested=1").fetchall():
                self._finalize(db, row, 'cancelled', 'cancelled_by_owner', None)


def _statements(sql: str):
    buffer = ''
    for line in sql.splitlines(keepends=True):
        if not buffer and line.strip().startswith('--'):
            continue
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                yield buffer.strip()
            buffer = ''
    if buffer.strip():
        raise ValueError('incomplete SQL statement in migration')


def _adapter(row) -> dict:
    return {'id': row['id'], 'name': row['name'], 'script': row['script'], 'args': json.loads(row['args_json']),
            'script_sha256': row['script_sha256'], 'granted': json.loads(row['granted_json']),
            'created_utc': row['created_utc'], 'updated_utc': row['updated_utc'], 'revoked_utc': row['revoked_utc'],
            'revoked': row['revoked_utc'] is not None, 'rev': row['rev']}


def _event(row) -> dict:
    return {'run_id': row['run_id'], 'seq': row['seq'], 'utc': row['utc'], 'type': row['type'],
            'stage_key': row['stage_key'], 'demo': bool(row['demo']), 'data': json.loads(row['data_json'])}


def _run(row, stages, artifacts, *, full):
    demo = bool(row['demo'])
    result = {'id': row['id'], 'version_id': row['version_id'], 'definition_id': row['definition_id'],
              'status': row['status'], 'demo': demo, 'label': 'DEMO' if demo else None, 'reason': row['reason'],
              'snapshot_sha256': row['snapshot_sha256'], 'created_utc': row['created_utc'],
              'started_utc': row['started_utc'], 'ended_utc': row['ended_utc'], 'heartbeat_utc': row['heartbeat_utc'],
              'lease_until': row['lease_until'], 'exit_code': row['exit_code'], 'attempt': row['attempt'],
              'rev': row['rev'], 'relaunch_of': row['relaunch_of'], 'cancel_requested': bool(row['cancel_requested']),
              'schema_version': schema.SCHEMA_VERSION}
    evidence = json.loads(row['evidence_json']) if row['evidence_json'] else None
    result['evidence'] = evidence or {'events_sha256': None, 'artifacts': []}
    if full:
        result['snapshot'] = json.loads(row['snapshot_json'])
        result['overrides'] = json.loads(row['overrides_json'])
        result['stages'] = [{'id': s['id'], 'run_id': s['run_id'], 'key': s['key'], 'status': s['status'],
                             'attempt': s['attempt'], 'started_utc': s['started_utc'], 'ended_utc': s['ended_utc'],
                             'exit_code': s['exit_code'], 'reason': s['reason'], 'tokens': s['tokens'],
                             'cost_usd_cents': s['cost_cents']} for s in stages]
        result['artifacts'] = [{'id': a['id'], 'run_id': a['run_id'], 'stage_key': a['stage_key'], 'name': a['name'],
                                'media_type': a['media_type'], 'size': a['size'], 'sha256': a['sha256'],
                                'created_utc': a['created_utc'], 'demo': bool(a['demo']),
                                'flagged': bool(a['flagged'])} for a in artifacts]
    return result
