"""Orchestration persistence (AHS-05): stage attempt claims, checkpoints, resume and the hash-chained audit log.

A mixin of ``Store``: every method here runs one transaction through ``Store.tx`` and appends the run events and
audit entries that describe the change in that same transaction. Stage rows are per attempt; the (run, stage,
attempt) idempotency key plus a compare-and-set on ``status='queued'`` makes a duplicate claim a no-op.
"""
from __future__ import annotations

import json

from aether.studio import scanner, schema
from aether.studio.schema import canonical_json, sha256_hex

CHAIN_ROOT = '0' * 64
MAX_ATTEMPT_ROW = 8
AUDIT_TEXT_MAX = 300
LEASE = "SELECT 1 FROM runs WHERE id=? AND status='running' AND lease_owner=?"


def idempotency_key(run_id: str, key: str, attempt: int) -> str:
    return sha256_hex(f'{run_id}:{key}:{attempt}')


def audit_hash(prev: str, run_id, seq, utc, actor, action, stage_key, detail_json: str) -> str:
    return sha256_hex(canonical_json([prev, run_id, seq, utc, actor, action, stage_key, detail_json]))


def clean_detail(value):
    """Audit details are plain JSON; free text is truncated and secret-shaped text redacted (no secrets in entries)."""
    if isinstance(value, str):
        return scanner.redact(value[:AUDIT_TEXT_MAX])[0]
    if isinstance(value, dict):
        return {str(k): clean_detail(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_detail(v) for v in value]
    assert value is None or isinstance(value, (bool, int, float)), f'audit detail not JSON: {type(value).__name__}'
    return value


class OrchestrationMixin:
    # ------------------------------------------------------------ audit
    def _audit(self, db, run_id, actor, action, stage_key=None, detail=None):
        last = db.execute('SELECT seq, hash FROM run_audit WHERE run_id=? ORDER BY seq DESC LIMIT 1', (run_id,)).fetchone()
        seq, prev = (last['seq'] + 1, last['hash']) if last else (1, CHAIN_ROOT)
        detail_json = canonical_json(clean_detail(detail or {}))
        utc = self.now()
        db.execute('INSERT INTO run_audit(run_id, seq, utc, actor, action, stage_key, detail_json, prev_hash, hash) '
                   'VALUES (?,?,?,?,?,?,?,?,?)', (run_id, seq, utc, actor, action, stage_key, detail_json, prev,
                                                  audit_hash(prev, run_id, seq, utc, actor, action, stage_key, detail_json)))

    def audit_event(self, run_id, owner, actor, action, stage_key=None, detail=None):
        """Lease-guarded standalone audit entry (budget, cancel, verdict ...). False if the lease is not ours."""
        with self.tx() as db:
            if not db.execute(LEASE, (run_id, owner)).fetchone():
                return False
            self._audit(db, run_id, actor, action, stage_key, detail)
            return True

    def audit(self, run_id):
        with self._lock:
            self._require_run(run_id)
            rows = self._db.execute('SELECT * FROM run_audit WHERE run_id=? ORDER BY seq', (run_id,)).fetchall()
        return [{'seq': r['seq'], 'utc': r['utc'], 'actor': r['actor'], 'action': r['action'], 'stage_key': r['stage_key'],
                 'detail': json.loads(r['detail_json']), 'prev_hash': r['prev_hash'], 'hash': r['hash']} for r in rows]

    def verify_audit(self, run_id) -> dict:
        """Recompute the chain. ``broken_seq`` is the first entry that does not verify (None when intact)."""
        prev, count = CHAIN_ROOT, 0
        with self._lock:
            self._require_run(run_id)
            rows = self._db.execute('SELECT * FROM run_audit WHERE run_id=? ORDER BY seq', (run_id,)).fetchall()
        for row in rows:
            count += 1
            expected = audit_hash(prev, run_id, count, row['utc'], row['actor'], row['action'], row['stage_key'],
                                  row['detail_json'])
            if row['seq'] != count or row['prev_hash'] != prev or row['hash'] != expected:
                return {'ok': False, 'entries': len(rows), 'broken_seq': row['seq'], 'head': None}
            prev = row['hash']
        return {'ok': True, 'entries': count, 'broken_seq': None, 'head': prev if count else None}

    def _audit_head(self, db, run_id):
        row = db.execute('SELECT seq, hash FROM run_audit WHERE run_id=? ORDER BY seq DESC LIMIT 1', (run_id,)).fetchone()
        return {'entries': row['seq'], 'head': row['hash']} if row else {'entries': 0, 'head': None}

    # ------------------------------------------------------------ graph
    def expansions(self, run_id):
        with self._lock:
            rows = self._db.execute('SELECT * FROM run_expansions WHERE run_id=? ORDER BY rowid', (run_id,)).fetchall()
        return [{'planner_key': r['planner_key'], 'stages': json.loads(r['stages_json'])} for r in rows]

    def graph(self, run_id):
        """Effective stage specs: the immutable snapshot's plus any validated planner expansions."""
        row = self.run_row(run_id)
        stages = json.loads(row['snapshot_json'])['stages']
        for item in self.expansions(run_id):
            stages = stages + item['stages']
        return stages

    def granted_capabilities(self, run_id, stage_key):
        with self._lock:
            rows = self._db.execute("SELECT capability FROM approvals WHERE run_id=? AND stage_key=? AND status='granted'",
                                    (run_id, stage_key)).fetchall()
        return tuple(sorted({r['capability'] for r in rows}))

    def stage_artifacts(self, run_id, keys):
        marks = ','.join('?' * len(keys))
        with self._lock:
            rows = self._db.execute(f'SELECT * FROM artifacts WHERE run_id=? AND stage_key IN ({marks}) ORDER BY rowid',
                                    (run_id, *keys)).fetchall()
        return [{'id': r['id'], 'stage_key': r['stage_key'], 'name': r['name'], 'media_type': r['media_type'],
                 'sha256': r['sha256'], 'size': r['size'], 'path': r['path']} for r in rows]

    def usage(self, run_id):
        with self._lock:
            row = self._db.execute('SELECT COALESCE(SUM(tokens), 0) AS t, COALESCE(SUM(cost_cents), 0) AS c '
                                   'FROM stages WHERE run_id=?', (run_id,)).fetchone()
        return {'tokens': row['t'], 'cost_usd_cents': row['c']}

    # ------------------------------------------------------------ stage attempts
    def _stage_row(self, db, run_id, key, attempt):
        return db.execute('SELECT * FROM stages WHERE run_id=? AND key=? AND attempt=?', (run_id, key, attempt)).fetchone()

    def _insert_attempt(self, db, run_id, key, attempt):
        assert 1 <= attempt <= MAX_ATTEMPT_ROW, f'attempt {attempt} out of range'
        db.execute("INSERT INTO stages(id, run_id, key, status, attempt) VALUES (?,?,?,'queued',?)",
                   (schema.new_id('stg', now=self.clock()), run_id, key, attempt))
        db.execute('UPDATE runs SET attempt=MAX(attempt, ?) WHERE id=?', (attempt, run_id))

    def claim_stage(self, run_id, owner, key, attempt):
        """Compare-and-set queued -> running under the run lease. A duplicate claim returns False and executes nothing."""
        with self.tx() as db:
            if not db.execute(LEASE, (run_id, owner)).fetchone():
                return False
            row = self._stage_row(db, run_id, key, attempt)
            if row is None:
                return False
            idem = idempotency_key(run_id, key, attempt)
            cur = db.execute("UPDATE stages SET status='running', started_utc=?, claimed_by=?, idem_key=?, rev=rev+1 "
                             "WHERE id=? AND status='queued' AND rev=?", (self.now(), owner, idem, row['id'], row['rev']))
            if cur.rowcount != 1:
                return False
            self._append(db, run_id, 'stage.status', {'from': 'queued', 'to': 'running', 'attempt': attempt,
                                                      'reason': None, 'exit_code': None}, stage_key=key)
            self._audit(db, run_id, 'runner', 'stage.scheduled', key, {'attempt': attempt, 'idempotency_key': idem})
            return True

    def end_stage(self, run_id, owner, key, attempt, to, *, reason=None, exit_code=None, retry=False, expansion=None,
                  detail=None):
        """running -> succeeded|failed|cancelled|blocked|interrupted. ``retry`` creates the next attempt row and
        ``expansion`` (already validated stages) is persisted in the same transaction, so a crash cannot split them."""
        assert to in ('succeeded', 'failed', 'cancelled', 'blocked'), to
        with self.tx() as db:
            if not db.execute(LEASE, (run_id, owner)).fetchone():
                return False
            row = self._stage_row(db, run_id, key, attempt)
            cur = db.execute("UPDATE stages SET status=?, reason=?, exit_code=?, ended_utc=?, rev=rev+1 "
                             "WHERE id=? AND status='running' AND rev=?",
                             (to, reason, exit_code, self.now(), row['id'], row['rev']))
            if cur.rowcount != 1:
                return False
            self._append(db, run_id, 'stage.status', {'from': 'running', 'to': to, 'attempt': attempt,
                                                      'reason': reason, 'exit_code': exit_code}, stage_key=key)
            self._audit(db, run_id, 'runner', f'stage.{to}', key, dict(detail or {}, attempt=attempt, reason=reason))
            if retry:
                self._insert_attempt(db, run_id, key, attempt + 1)
                self._audit(db, run_id, 'runner', 'stage.retried', key, {'next_attempt': attempt + 1, 'reason': reason})
            if expansion:
                db.execute('INSERT INTO run_expansions(run_id, planner_key, stages_json, created_utc) VALUES (?,?,?,?)',
                           (run_id, key, canonical_json(expansion), self.now()))
                for stage in expansion:
                    self._insert_attempt(db, run_id, stage['key'], 1)
                self._append(db, run_id, 'plan.expanded', {'planner': key, 'added': [s['key'] for s in expansion]},
                             stage_key=key)
                self._audit(db, run_id, f'planner:{key}', 'plan.expanded', key,
                            {'added': [s['key'] for s in expansion], 'total_stages': self._count_stages(db, run_id)})
            return True

    def _count_stages(self, db, run_id):
        return db.execute('SELECT COUNT(DISTINCT key) FROM stages WHERE run_id=?', (run_id,)).fetchone()[0]

    def settle_pending(self, run_id, owner, items):
        """Move still-queued stages to skipped|cancelled. ``items`` = [(key, status, reason)]. Queued rows only."""
        with self.tx() as db:
            if not db.execute(LEASE, (run_id, owner)).fetchone():
                return False
            for key, to, reason in items:
                assert to in ('skipped', 'cancelled'), to
                row = db.execute("SELECT * FROM stages WHERE run_id=? AND key=? AND status='queued' ORDER BY attempt DESC "
                                 'LIMIT 1', (run_id, key)).fetchone()
                assert row is not None, f'no queued attempt for {key}'
                db.execute('UPDATE stages SET status=?, reason=?, ended_utc=?, rev=rev+1 WHERE id=?',
                           (to, reason, self.now(), row['id']))
                self._append(db, run_id, 'stage.status', {'from': 'queued', 'to': to, 'attempt': row['attempt'],
                                                          'reason': reason, 'exit_code': None}, stage_key=key)
                self._audit(db, run_id, 'runner', f'stage.{to}', key, {'attempt': row['attempt'], 'reason': reason})
            return True

    def requeue_stage(self, run_id, owner, key, why):
        """Resume: a blocked/interrupted latest attempt gets a fresh queued attempt (the old row stays as history)."""
        with self.tx() as db:
            if not db.execute(LEASE, (run_id, owner)).fetchone():
                return False
            row = db.execute('SELECT * FROM stages WHERE run_id=? AND key=? ORDER BY attempt DESC LIMIT 1',
                             (run_id, key)).fetchone()
            assert row['status'] in ('blocked', 'interrupted'), f'{key} is {row["status"]}'
            self._insert_attempt(db, run_id, key, row['attempt'] + 1)
            self._audit(db, run_id, 'runner', 'stage.resumed', key, {'from': row['status'], 'attempt': row['attempt'] + 1,
                                                                    'why': why})
            return True

    def add_usage(self, run_id, owner, key, attempt, tokens, cost_cents):
        with self.tx() as db:
            if not db.execute(LEASE, (run_id, owner)).fetchone():
                return False
            db.execute('UPDATE stages SET tokens=tokens+?, cost_cents=cost_cents+? WHERE run_id=? AND key=? AND attempt=?',
                       (tokens, cost_cents, run_id, key, attempt))
            self._append(db, run_id, 'stage.usage', {'tokens': tokens, 'cost_usd_cents': cost_cents, 'attempt': attempt},
                         stage_key=key)
            return True

    # ------------------------------------------------------------ resume / recovery
    def _resume(self, db, row, actor, why):
        db.execute("UPDATE runs SET status='queued', reason=NULL, lease_owner=NULL, lease_until=NULL, rev=rev+1 "
                   'WHERE id=?', (row['id'],))
        self._append(db, row['id'], 'run.status', {'from': row['status'], 'to': 'queued', 'reason': None})
        self._audit(db, row['id'], actor, 'run.resumed', None, {'from': row['status'], 'reason': row['reason'], 'why': why})

    def resume_run(self, run_id):
        """Owner action: blocked -> queued; the dispatcher re-claims it and the driver continues from the checkpoint."""
        from aether.studio.store import StoreError
        with self.tx() as db:
            row = self._require_run(run_id, db)
            if row['status'] != 'blocked':
                raise StoreError(f'run is {row["status"]}, only a blocked run can be resumed')
            if db.execute("SELECT 1 FROM approvals WHERE run_id=? AND status='pending'", (run_id,)).fetchone():
                raise StoreError('an approval is still pending; decide it first')
            self._resume(db, row, 'owner', 'owner_resume')
        return 'queued'

    def recover_expired_leases(self):
        """Crash recovery for runs whose lease expired. Completed stages are never touched. A stage that was running is
        marked ``interrupted``; the run resumes only if every such stage is ``restartable`` and was not interrupted
        before, otherwise it goes ``blocked`` (owner_action_required). Returns {run_id: 'queued'|'blocked'|'cancelled'}."""
        out = {}
        with self.tx() as db:
            for row in db.execute("SELECT * FROM runs WHERE status='running' AND (lease_until IS NULL OR lease_until < ?)",
                                  (self.now(),)).fetchall():
                run_id = row['id']
                specs = {s['key']: s for s in self._specs(db, row)}
                unsafe = []
                for stage in db.execute("SELECT * FROM stages WHERE run_id=? AND status='running'", (run_id,)).fetchall():
                    db.execute("UPDATE stages SET status='interrupted', reason='lease_lost', ended_utc=?, rev=rev+1 WHERE id=?",
                               (self.now(), stage['id']))
                    self._append(db, run_id, 'stage.status', {'from': 'running', 'to': 'interrupted',
                                                              'attempt': stage['attempt'], 'reason': 'lease_lost',
                                                              'exit_code': None}, stage_key=stage['key'])
                    prior = db.execute("SELECT COUNT(*) FROM stages WHERE run_id=? AND key=? AND status='interrupted' "
                                       'AND attempt<?', (run_id, stage['key'], stage['attempt'])).fetchone()[0]
                    restartable = specs[stage['key']].get('restartable', False) and prior == 0
                    self._audit(db, run_id, 'runner', 'stage.interrupted', stage['key'],
                                {'attempt': stage['attempt'], 'restartable': restartable})
                    if not restartable:
                        unsafe.append(stage['key'])
                if row['cancel_requested']:
                    self._finalize(db, row, 'cancelled', 'cancelled_by_owner', None)
                    out[run_id] = 'cancelled'
                elif unsafe:
                    db.execute("UPDATE runs SET status='blocked', reason='owner_action_required', lease_owner=NULL, "
                               'lease_until=NULL, rev=rev+1 WHERE id=?', (run_id,))
                    self._append(db, run_id, 'run.status', {'from': 'running', 'to': 'blocked',
                                                            'reason': 'owner_action_required'})
                    self._audit(db, run_id, 'runner', 'run.blocked', None, {'reason': 'owner_action_required',
                                                                           'stages': sorted(unsafe)})
                    out[run_id] = 'blocked'
                else:
                    self._resume(db, row, 'runner', 'lease_expired')
                    out[run_id] = 'queued'
        return out

    def _specs(self, db, row):
        stages = json.loads(row['snapshot_json'])['stages']
        for item in db.execute('SELECT stages_json FROM run_expansions WHERE run_id=? ORDER BY rowid', (row['id'],)):
            stages = stages + json.loads(item['stages_json'])
        return stages
