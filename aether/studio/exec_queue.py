"""Persistent run queue for the CLI executor (CLI_EXECUTOR.md). Exists only with ``AETHER_EXEC=cli``.

The tables are created by ``QueueStore.ensure()`` (called only when the flag is on), with the same transaction
helper as the rest of the Studio database; flag off = no table, no thread, no route. State kept here:

* ``queue``: one row per queued CLI run (state queued|running|blocked|done|cancelled|failed). The prompt is not in
  SQLite: ``prompt_ref`` names an owner-only file in ``<data dir>/queue-prompts``.
* ``queue_attempts``: one row per launch (slot, outcome); ``queue_events``: provider_switch and state events.
* ``slot_state``: ``active_count`` is authoritative here; ``blocked_until`` / ``last_limit_at`` are a *mirror*.
  The single source of truth for a slot block is ``slots.json`` (written by the executor, see limits/slots.py).
* ``queue_audit``: append-only, hash-chained (same hash function as ``run_audit``); the existing audit chain is
  per Studio run and has a foreign key to ``runs``, so a queue item (which is not a Studio run) gets its own chain.
"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
import threading

from aether.studio import fsperm, scanner
from aether.studio.orchestration_store import CHAIN_ROOT, audit_hash, clean_detail
from aether.studio.schema import canonical_json, new_id
from aether.studio.store import _statements

STATES = ('queued', 'running', 'blocked', 'done', 'cancelled', 'failed')
OPEN_STATES = ('queued', 'running', 'blocked')
MAX_OPEN = 200
PROMPT_MAX = 200_000
PAUSE_KEY = 'paused'
SQL = """
CREATE TABLE IF NOT EXISTS queue (
  run_id TEXT PRIMARY KEY,
  position INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('queued','running','blocked','done','cancelled','failed')),
  reason TEXT,
  requested_slot TEXT NOT NULL DEFAULT 'auto',
  workspace TEXT NOT NULL,
  prompt_ref TEXT,
  blueprint_ref TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS queue_attempts (
  run_id TEXT NOT NULL REFERENCES queue(run_id),
  attempt INTEGER NOT NULL,
  slot_id TEXT NOT NULL,
  state TEXT NOT NULL,
  reason TEXT,
  exit_code INTEGER,
  blocked_until TEXT,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  PRIMARY KEY (run_id, attempt)
);
CREATE TABLE IF NOT EXISTS queue_events (
  run_id TEXT NOT NULL REFERENCES queue(run_id),
  seq INTEGER NOT NULL,
  utc TEXT NOT NULL,
  type TEXT NOT NULL,
  data_json TEXT NOT NULL,
  PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS slot_state (
  slot_id TEXT PRIMARY KEY,
  blocked_until TEXT,
  last_limit_at TEXT,
  active_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS queue_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS queue_audit (
  seq INTEGER PRIMARY KEY,
  utc TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  run_id TEXT,
  detail_json TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS queue_track (
  run_id TEXT PRIMARY KEY REFERENCES queue(run_id),
  slot_id TEXT,
  slot_kind TEXT,
  started_at TEXT,
  ended_at TEXT,
  wall_seconds REAL,
  tokens INTEGER,
  cost_cents INTEGER,
  final_status TEXT,
  log_sha256 TEXT,
  result_sha256 TEXT,
  updated_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS queue_audit_no_update BEFORE UPDATE ON queue_audit
BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER IF NOT EXISTS queue_audit_no_delete BEFORE DELETE ON queue_audit
BEGIN SELECT RAISE(ABORT, 'append_only'); END;
"""
TABLES = ('queue', 'queue_attempts', 'queue_events', 'slot_state', 'queue_meta', 'queue_audit', 'queue_track')
# Columns added to ``queue_attempts`` after K2 (K3 tracking); applied by ``ensure`` only when missing.
ATTEMPT_COLUMNS = (('slot_kind', 'TEXT'), ('tokens', 'INTEGER'), ('cost_cents', 'INTEGER'))
TERMINAL = ('done', 'cancelled', 'failed')
OUTCOMES = {'succeeded': 'ok', 'failed': 'failed', 'interrupted': 'failed', 'limit_stop': 'limit_stop', 'cancelled': 'cancelled'}
DATE_FILTER = re.compile(r'^\d{4}-\d{2}-\d{2}(T\d{2}(:\d{2}(:\d{2}(\.\d{1,6})?)?)?Z?)?$')
SLOT_FILTER = re.compile(r'^[a-z][a-z0-9_-]{0,39}$')
TRACKER_MAX = 200


def _epoch(text):
    return datetime.strptime(text, '%Y-%m-%dT%H:%M:%S.%fZ').timestamp()


class QueueError(ValueError):
    def __init__(self, code, detail='', status=400):
        super().__init__(f'{code} {detail}'.strip())
        self.code, self.detail, self.status = code, detail, status


class QueueStore:
    def __init__(self, store, data_dir):
        self.store = store
        self.data_dir = Path(data_dir)
        self.prompt_dir = self.data_dir / 'queue-prompts'
        self._lock = threading.RLock()

    def ensure(self):
        with self.store.tx() as db:
            for statement in _statements(SQL):
                db.execute(statement)
            have = {r['name'] for r in db.execute('PRAGMA table_info(queue_attempts)')}
            for name, kind in ATTEMPT_COLUMNS:
                if name not in have:
                    db.execute(f'ALTER TABLE queue_attempts ADD COLUMN {name} {kind}')
        fsperm.private_dir(self.prompt_dir)

    @staticmethod
    def tables_exist(store) -> list:
        with store._lock:
            rows = store._db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return [r['name'] for r in rows if r['name'] in TABLES]

    # ------------------------------------------------------------ audit
    def _audit(self, db, actor, action, run_id=None, detail=None):
        last = db.execute('SELECT seq, hash FROM queue_audit ORDER BY seq DESC LIMIT 1').fetchone()
        seq, prev = (last['seq'] + 1, last['hash']) if last else (1, CHAIN_ROOT)
        detail_json = canonical_json(clean_detail(detail or {}))
        utc = self.store.now()
        db.execute('INSERT INTO queue_audit(seq, utc, actor, action, run_id, detail_json, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?)',
                   (seq, utc, actor, action, run_id, detail_json, prev,
                    audit_hash(prev, 'queue', seq, utc, actor, action, run_id, detail_json)))

    def audit(self, actor, action, run_id=None, detail=None):
        with self.store.tx() as db:
            self._audit(db, actor, action, run_id, detail)

    def audit_entries(self, limit=200):
        with self.store._lock:
            rows = self.store._db.execute('SELECT * FROM queue_audit ORDER BY seq DESC LIMIT ?', (limit,)).fetchall()
        return [dict(r, detail=json.loads(r['detail_json'])) for r in reversed(rows)]

    def verify_audit(self) -> dict:
        with self.store._lock:
            rows = self.store._db.execute('SELECT * FROM queue_audit ORDER BY seq').fetchall()
        prev = CHAIN_ROOT
        for i, row in enumerate(rows, 1):
            expect = audit_hash(prev, 'queue', row['seq'], row['utc'], row['actor'], row['action'], row['run_id'],
                                row['detail_json'])
            if row['seq'] != i or row['prev_hash'] != prev or row['hash'] != expect:
                return {'ok': False, 'entries': len(rows), 'broken_seq': row['seq'], 'head': prev}
            prev = row['hash']
        return {'ok': True, 'entries': len(rows), 'broken_seq': None, 'head': prev}

    # ------------------------------------------------------------ items
    def _item(self, row):
        return {'run_id': row['run_id'], 'position': row['position'], 'state': row['state'], 'reason': row['reason'],
                'requested_slot': row['requested_slot'], 'workspace': row['workspace'],
                'prompt_ref': row['prompt_ref'], 'blueprint_ref': row['blueprint_ref'],
                'created_at': row['created_at'], 'updated_at': row['updated_at']}

    def add(self, workspace, prompt, requested_slot='auto', blueprint_ref=None, actor='owner') -> dict:
        run_id = new_id('que')
        ref = f'{run_id}.txt'
        with self._lock:
            with self.store.tx() as db:
                if db.execute("SELECT COUNT(*) FROM queue WHERE state IN ('queued','running','blocked')").fetchone()[0] >= MAX_OPEN:
                    raise QueueError('queue_full', f'more than {MAX_OPEN} open items', 429)
                pos = db.execute('SELECT COALESCE(MAX(position), 0) + 1 FROM queue').fetchone()[0]
                now = self.store.now()
                path = self.prompt_dir / ref
                fsperm.private_dir(self.prompt_dir)
                path.write_text(prompt, encoding='utf-8')
                fsperm.restrict(path, directory=False)
                db.execute('INSERT INTO queue(run_id, position, state, requested_slot, workspace, prompt_ref, blueprint_ref, '
                           "created_at, updated_at) VALUES (?,?, 'queued', ?,?,?,?,?,?)",
                           (run_id, pos, requested_slot, workspace, ref, blueprint_ref, now, now))
                self._audit(db, actor, 'queue.add', run_id, {'workspace': workspace, 'slot': requested_slot,
                                                              'blueprint_ref': blueprint_ref, 'prompt_chars': len(prompt)})
        return self.get(run_id)

    def get(self, run_id) -> dict:
        with self.store._lock:
            row = self.store._db.execute('SELECT * FROM queue WHERE run_id=?', (run_id,)).fetchone()
            if row is None:
                raise QueueError('not_found', run_id, 404)
            item = self._item(row)
            item['attempts'] = [dict(r) for r in self.store._db.execute(
                'SELECT attempt, slot_id, state, reason, exit_code, blocked_until, started_at, ended_at '
                'FROM queue_attempts WHERE run_id=? ORDER BY attempt', (run_id,))]
            item['events'] = [{'seq': r['seq'], 'utc': r['utc'], 'type': r['type'], **json.loads(r['data_json'])}
                              for r in self.store._db.execute('SELECT * FROM queue_events WHERE run_id=? ORDER BY seq', (run_id,))]
        return item

    def list(self, state=None) -> list:
        with self.store._lock:
            rows = self.store._db.execute(
                'SELECT * FROM queue' + (' WHERE state=?' if state else '') + ' ORDER BY position, created_at',
                (state,) if state else ()).fetchall()
        return [self._item(r) for r in rows]

    def prompt(self, item) -> str:
        return (self.prompt_dir / item['prompt_ref']).read_text(encoding='utf-8')

    def next_queued(self, *, skip=()) -> dict | None:
        with self.store._lock:
            for row in self.store._db.execute("SELECT * FROM queue WHERE state='queued' ORDER BY position, created_at"):
                if row['run_id'] not in skip:
                    return self._item(row)
        return None

    def queued(self) -> list:
        return self.list('queued')

    def set_state(self, run_id, state, reason=None, *, expect=None, front=False, audit=None, detail=None, actor='driver') -> bool:
        """Compare-and-set on the old state (``expect``: tuple). ``front`` puts a re-queued item before every other."""
        assert state in STATES
        with self.store.tx() as db:
            row = db.execute('SELECT state FROM queue WHERE run_id=?', (run_id,)).fetchone()
            if row is None:
                raise QueueError('not_found', run_id, 404)
            if expect is not None and row['state'] not in expect:
                return False
            if front:
                db.execute('UPDATE queue SET position=(SELECT COALESCE(MIN(position), 1) - 1 FROM queue) WHERE run_id=?', (run_id,))
            db.execute('UPDATE queue SET state=?, reason=?, updated_at=? WHERE run_id=?', (state, reason, self.store.now(), run_id))
            self._event(db, run_id, 'state', {'from': row['state'], 'to': state, 'reason': reason})
            if audit:
                self._audit(db, actor, audit, run_id, dict(detail or {}, **{'from': row['state'], 'to': state, 'reason': reason}))
            self._refresh_track(db, run_id)
        return True

    def reorder(self, ids, actor='owner'):
        with self.store.tx() as db:
            queued = [r['run_id'] for r in db.execute("SELECT run_id FROM queue WHERE state='queued' ORDER BY position, created_at")]
            if sorted(ids) != sorted(queued) or len(set(ids)) != len(ids):
                raise QueueError('invalid_request', 'ids must list every queued item exactly once')
            start = db.execute('SELECT COALESCE(MAX(position), 0) FROM queue').fetchone()[0]
            for i, run_id in enumerate(ids, 1):
                db.execute('UPDATE queue SET position=?, updated_at=? WHERE run_id=?', (start + i, self.store.now(), run_id))
            self._audit(db, actor, 'queue.reorder', None, {'order': list(ids)})

    # ------------------------------------------------------------ attempts and events
    def _event(self, db, run_id, type_, data):
        seq = db.execute('SELECT COALESCE(MAX(seq), 0) + 1 FROM queue_events WHERE run_id=?', (run_id,)).fetchone()[0]
        db.execute('INSERT INTO queue_events(run_id, seq, utc, type, data_json) VALUES (?,?,?,?,?)',
                   (run_id, seq, self.store.now(), type_, canonical_json(data)))

    def event(self, run_id, type_, data):
        with self.store.tx() as db:
            self._event(db, run_id, type_, data)

    def start_attempt(self, run_id, slot_id, slot_kind=None) -> int:
        """queued -> running, new attempt row, slot active_count + 1, all in one transaction. Returns attempt number."""
        with self.store.tx() as db:
            row = db.execute('SELECT state FROM queue WHERE run_id=?', (run_id,)).fetchone()
            if row is None or row['state'] != 'queued':
                raise QueueError('conflict', 'item is not queued', 409)
            n = db.execute('SELECT COALESCE(MAX(attempt), 0) + 1 FROM queue_attempts WHERE run_id=?', (run_id,)).fetchone()[0]
            now = self.store.now()
            db.execute("INSERT INTO queue_attempts(run_id, attempt, slot_id, slot_kind, state, started_at) VALUES (?,?,?,?, 'running', ?)",
                       (run_id, n, slot_id, slot_kind, now))
            db.execute("UPDATE queue SET state='running', reason=NULL, updated_at=? WHERE run_id=?", (now, run_id))
            db.execute('INSERT INTO slot_state(slot_id, active_count) VALUES (?, 1) '
                       'ON CONFLICT(slot_id) DO UPDATE SET active_count=active_count+1', (slot_id,))
            self._event(db, run_id, 'attempt.started', {'attempt': n, 'slot': slot_id})
            self._audit(db, 'driver', 'driver.start', run_id, {'attempt': n, 'slot': slot_id, 'kind': slot_kind})
            self._refresh_track(db, run_id)
        return n

    def end_attempt(self, run_id, attempt, state, *, reason=None, exit_code=None, blocked_until=None):
        with self.store.tx() as db:
            row = db.execute('SELECT slot_id, state FROM queue_attempts WHERE run_id=? AND attempt=?', (run_id, attempt)).fetchone()
            if row is None or row['state'] != 'running':
                return
            db.execute('UPDATE queue_attempts SET state=?, reason=?, exit_code=?, blocked_until=?, ended_at=? WHERE run_id=? AND attempt=?',
                       (state, reason, exit_code, blocked_until, self.store.now(), run_id, attempt))
            db.execute('UPDATE slot_state SET active_count=MAX(0, active_count-1) WHERE slot_id=?', (row['slot_id'],))
            self._event(db, run_id, 'attempt.ended', {'attempt': attempt, 'slot': row['slot_id'], 'state': state,
                                                      'reason': reason, 'exit_code': exit_code})
            self._audit(db, 'driver', f'driver.{state}', run_id, {'attempt': attempt, 'slot': row['slot_id'], 'reason': reason,
                                                                  'exit_code': exit_code, 'blocked_until': blocked_until})
            self._refresh_track(db, run_id)

    # ------------------------------------------------------------ tracking (K3)
    def _refresh_track(self, db, run_id):
        """Recompute the tracking row from attempts: figures exist only when the CLI reported them (NULL otherwise)."""
        state = db.execute('SELECT state FROM queue WHERE run_id=?', (run_id,)).fetchone()
        attempts = db.execute('SELECT * FROM queue_attempts WHERE run_id=? ORDER BY attempt', (run_id,)).fetchall()
        if state is None or not attempts:
            return
        last = attempts[-1]
        wall = sum(_epoch(a['ended_at']) - _epoch(a['started_at']) for a in attempts if a['ended_at'])
        tokens = [a['tokens'] for a in attempts if a['tokens'] is not None]
        costs = [a['cost_cents'] for a in attempts if a['cost_cents'] is not None]
        terminal = state['state'] in TERMINAL
        db.execute(
            'INSERT INTO queue_track(run_id, slot_id, slot_kind, started_at, ended_at, wall_seconds, tokens, cost_cents, '
            'final_status, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET slot_id=excluded.slot_id, '
            'slot_kind=excluded.slot_kind, started_at=excluded.started_at, ended_at=excluded.ended_at, '
            'wall_seconds=excluded.wall_seconds, tokens=excluded.tokens, cost_cents=excluded.cost_cents, '
            'final_status=excluded.final_status, updated_at=excluded.updated_at',
            (run_id, last['slot_id'], last['slot_kind'], attempts[0]['started_at'],
             last['ended_at'] if terminal else None, round(wall, 3), sum(tokens) if tokens else None,
             sum(costs) if costs else None, state['state'], self.store.now()))

    def record_usage(self, run_id, attempt, tokens=None, cost_cents=None):
        """Usage the CLI reported for an attempt (latest report wins). ``None`` = not reported, stays NULL."""
        if tokens is None and cost_cents is None:
            return
        with self.store.tx() as db:
            db.execute('UPDATE queue_attempts SET tokens=COALESCE(?, tokens), cost_cents=COALESCE(?, cost_cents) '
                       'WHERE run_id=? AND attempt=?', (tokens, cost_cents, run_id, attempt))
            self._refresh_track(db, run_id)

    def set_evidence(self, run_id, log_sha256, result_sha256, actor='driver'):
        with self.store.tx() as db:
            if db.execute('SELECT 1 FROM queue_track WHERE run_id=?', (run_id,)).fetchone() is None:
                return
            db.execute('UPDATE queue_track SET log_sha256=?, result_sha256=?, updated_at=? WHERE run_id=?',
                       (log_sha256, result_sha256, self.store.now(), run_id))
            self._audit(db, actor, 'track.evidence', run_id, {'log_sha256': log_sha256, 'result_sha256': result_sha256})

    def _track_views(self, rows) -> list:
        """Tracking dicts for joined queue+queue_track rows (caller holds the store lock)."""
        db = self.store._db
        ids = [r['run_id'] for r in rows]
        marks = ','.join('?' * len(ids))
        attempts, switches = {}, {}
        if ids:
            for a in db.execute(f'SELECT * FROM queue_attempts WHERE run_id IN ({marks}) ORDER BY run_id, attempt', ids):
                attempts.setdefault(a['run_id'], []).append(
                    {'attempt': a['attempt'], 'slot': a['slot_id'], 'kind': a['slot_kind'], 'start': a['started_at'],
                     'end': a['ended_at'], 'outcome': OUTCOMES.get(a['state']), 'reason': a['reason'],
                     'tokens': a['tokens'], 'cost_usd_cents': a['cost_cents']})
            for e in db.execute(f"SELECT run_id, utc, data_json FROM queue_events WHERE type='provider_switch' "
                                f'AND run_id IN ({marks}) ORDER BY run_id, seq', ids):
                d = json.loads(e['data_json'])
                switches.setdefault(e['run_id'], []).append({'from': d.get('from'), 'to': d.get('to'),
                                                              'reason': d.get('reason'), 'at': e['utc']})
        views = []
        for r in rows:
            views.append({
                'run_id': r['run_id'], 'created_at': r['created_at'], 'workspace': r['workspace'],
                'requested_slot': r['requested_slot'], 'status': r['state'], 'reason': r['reason'],
                'provider': {'slot': r['t_slot'], 'kind': r['t_kind']} if r['t_slot'] else None,
                'attempts': attempts.get(r['run_id'], []), 'provider_switches': switches.get(r['run_id'], []),
                'started_at': r['t_start'], 'ended_at': r['t_end'], 'wall_seconds': r['t_wall'],
                'tokens': r['t_tokens'], 'cost_usd_cents': r['t_cost'],
                'evidence': {'log_sha256': r['t_log'], 'result_sha256': r['t_result']}})
        return views

    _TRACK_SELECT = ('SELECT q.*, t.slot_id AS t_slot, t.slot_kind AS t_kind, t.started_at AS t_start, t.ended_at AS t_end, '
                     't.wall_seconds AS t_wall, t.tokens AS t_tokens, t.cost_cents AS t_cost, t.log_sha256 AS t_log, '
                     't.result_sha256 AS t_result FROM queue q LEFT JOIN queue_track t ON t.run_id = q.run_id')

    def track(self, run_id) -> dict:
        with self.store._lock:
            rows = self.store._db.execute(self._TRACK_SELECT + ' WHERE q.run_id=?', (run_id,)).fetchall()
            if not rows:
                raise QueueError('not_found', run_id, 404)
            return self._track_views(rows)[0]

    def tracker(self, *, status=None, slot=None, kind=None, since=None, until=None, q=None, limit=50, offset=0) -> dict:
        where, args = [], []
        if status is not None:
            if status not in STATES:
                raise QueueError('invalid_request', 'status must be one of ' + ', '.join(STATES))
            where.append('q.state=?')
            args.append(status)
        if slot is not None:
            if not SLOT_FILTER.match(slot):
                raise QueueError('invalid_request', 'slot format')
            where.append('EXISTS (SELECT 1 FROM queue_attempts a WHERE a.run_id=q.run_id AND a.slot_id=?)')
            args.append(slot)
        if kind is not None:
            if kind not in ('claude', 'codex'):
                raise QueueError('invalid_request', 'kind must be claude or codex')
            where.append('EXISTS (SELECT 1 FROM queue_attempts a WHERE a.run_id=q.run_id AND a.slot_kind=?)')
            args.append(kind)
        for name, value, op in (('since', since, '>='), ('until', until, '<=')):
            if value is not None:
                if not DATE_FILTER.match(value):
                    raise QueueError('invalid_request', f'{name} must be YYYY-MM-DD or an ISO UTC time')
                where.append(f'substr(q.created_at, 1, {len(value)}) {op} ?')
                args.append(value)
        if q:
            like = '%' + q[:100].replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            esc = " ESCAPE '\\'"
            where.append(f"(q.run_id LIKE ?{esc} OR q.workspace LIKE ?{esc} OR q.requested_slot LIKE ?{esc} "
                         f"OR COALESCE(q.reason,'') LIKE ?{esc} OR EXISTS (SELECT 1 FROM queue_attempts a "
                         f"WHERE a.run_id=q.run_id AND a.slot_id LIKE ?{esc}))")
            args += [like] * 5
        clause = (' WHERE ' + ' AND '.join(where)) if where else ''
        limit = max(1, min(TRACKER_MAX, limit))
        offset = max(0, offset)
        with self.store._lock:
            db = self.store._db
            params = list(args)
            rows = db.execute(self._TRACK_SELECT + clause + ' ORDER BY q.created_at DESC, q.run_id LIMIT ? OFFSET ?',
                              params + [limit, offset]).fetchall()
            items = self._track_views(rows)
            frm = ' FROM queue q LEFT JOIN queue_track t ON t.run_id = q.run_id' + clause
            by_status = {r['state']: r['n'] for r in db.execute('SELECT q.state, COUNT(*) AS n' + frm + ' GROUP BY q.state', params)}
            agg = db.execute('SELECT COUNT(*) AS n, COALESCE(SUM(t.wall_seconds), 0) AS wall, SUM(t.tokens) AS tok, '
                             'COUNT(t.tokens) AS tok_n, SUM(t.cost_cents) AS cost, COUNT(t.cost_cents) AS cost_n' + frm, params).fetchone()
        return {'items': items, 'total': agg['n'], 'limit': limit, 'offset': offset,
                'summary': {'counts_by_status': {s: by_status.get(s, 0) for s in STATES},
                            'total_wall_seconds': round(agg['wall'], 3),
                            'tokens': {'sum': agg['tok'], 'reported_count': agg['tok_n']},
                            'cost_usd_cents': {'sum': agg['cost'], 'reported_count': agg['cost_n']}}}

    def last_attempt(self, run_id) -> dict | None:
        with self.store._lock:
            row = self.store._db.execute('SELECT * FROM queue_attempts WHERE run_id=? ORDER BY attempt DESC LIMIT 1', (run_id,)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------ slot_state (mirror + counters)
    def mirror_slots(self, slot_ids, state):
        """Copy blocked_until from slots.json state (the source of truth) into slot_state; informational only."""
        with self.store.tx() as db:
            for slot_id in slot_ids:
                entry = state.get(slot_id) or {}
                db.execute('INSERT INTO slot_state(slot_id, blocked_until) VALUES (?, ?) '
                           'ON CONFLICT(slot_id) DO UPDATE SET blocked_until=excluded.blocked_until',
                           (slot_id, entry.get('blocked_until')))

    def note_limit(self, slot_id, blocked_until):
        with self.store.tx() as db:
            db.execute('INSERT INTO slot_state(slot_id, blocked_until, last_limit_at) VALUES (?,?,?) '
                       'ON CONFLICT(slot_id) DO UPDATE SET blocked_until=excluded.blocked_until, last_limit_at=excluded.last_limit_at',
                       (slot_id, blocked_until, self.store.now()))

    def slot_rows(self) -> dict:
        with self.store._lock:
            return {r['slot_id']: dict(r) for r in self.store._db.execute('SELECT * FROM slot_state')}

    def active_count(self, slot_id) -> int:
        return (self.slot_rows().get(slot_id) or {}).get('active_count', 0)

    # ------------------------------------------------------------ pause
    def paused(self) -> bool:
        with self.store._lock:
            row = self.store._db.execute('SELECT value FROM queue_meta WHERE key=?', (PAUSE_KEY,)).fetchone()
        return bool(row and row['value'] == '1')

    def set_paused(self, value: bool, actor='owner'):
        with self.store.tx() as db:
            db.execute('INSERT INTO queue_meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       (PAUSE_KEY, '1' if value else '0'))
            self._audit(db, actor, 'queue.pause' if value else 'queue.resume', None, {})

    # ------------------------------------------------------------ restart recovery
    def recover(self) -> dict:
        """Startup: running attempts become ``interrupted`` and their items ``blocked`` (owner_action_required) until the
        owner resumes them (same rule as Store.recover_expired_leases for non-restartable stages). Queued stays queued;
        every ``active_count`` is reset to 0."""
        blocked = []
        with self.store.tx() as db:
            for row in db.execute("SELECT run_id FROM queue WHERE state='running'").fetchall():
                run_id = row['run_id']
                db.execute("UPDATE queue_attempts SET state='interrupted', reason='lease_lost', ended_at=? "
                           "WHERE run_id=? AND state='running'", (self.store.now(), run_id))
                db.execute("UPDATE queue SET state='blocked', reason='owner_action_required', updated_at=? WHERE run_id=?",
                           (self.store.now(), run_id))
                self._event(db, run_id, 'state', {'from': 'running', 'to': 'blocked', 'reason': 'owner_action_required'})
                self._audit(db, 'driver', 'driver.recover', run_id, {'to': 'blocked', 'reason': 'owner_action_required'})
                self._refresh_track(db, run_id)
                blocked.append(run_id)
            db.execute('UPDATE slot_state SET active_count=0')
        return {'blocked': blocked}


def clean_prompt(prompt) -> str:
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > PROMPT_MAX:
        raise QueueError('invalid_request', f'prompt must be 1..{PROMPT_MAX} characters')
    if scanner.redact(prompt)[1]:
        raise QueueError('secret_detected', 'prompt looks like it contains a secret; remove it')
    return prompt
