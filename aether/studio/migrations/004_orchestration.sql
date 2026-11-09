-- AHS-05 / 004: workflow orchestration. Additive: every existing row is copied unchanged.
-- foreign_keys=OFF
-- runs: reason CHECK gains the orchestration reasons; wall_used checkpoints cumulative wall seconds across resumes.
-- stages: status CHECK gains skipped/interrupted; attempt up to 8 (retries + one interrupted re-run); idem_key is the
-- per-(run, stage, attempt) idempotency key; claimed_by records the claimant; tokens/cost_cents are attempt usage.
CREATE TABLE runs_new (
  id TEXT PRIMARY KEY,
  version_id TEXT NOT NULL REFERENCES harness_versions(id),
  definition_id TEXT NOT NULL REFERENCES harness_definitions(id),
  snapshot_json TEXT NOT NULL,
  snapshot_sha256 TEXT NOT NULL,
  overrides_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('queued','running','succeeded','failed','cancelled','blocked')),
  demo INTEGER NOT NULL CHECK (demo IN (0, 1)),
  created_utc TEXT NOT NULL,
  started_utc TEXT,
  ended_utc TEXT,
  lease_owner TEXT,
  lease_until TEXT,
  heartbeat_utc TEXT,
  rev INTEGER NOT NULL DEFAULT 0,
  reason TEXT CHECK (reason IS NULL OR reason IN ('timeout','cancelled_by_owner','executor_error','exit_nonzero',
    'lease_lost','approval_denied','approval_pending','capability_unavailable','adapter_unavailable',
    'dependency_failed','limit_exceeded','invalid_snapshot',
    'budget_exceeded','owner_action_required','expansion_rejected','review_rejected')),
  exit_code INTEGER,
  attempt INTEGER NOT NULL DEFAULT 1,
  cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
  relaunch_of TEXT REFERENCES runs(id),
  events_sha256 TEXT,
  evidence_json TEXT,
  wall_used REAL NOT NULL DEFAULT 0
);
INSERT INTO runs_new(id, version_id, definition_id, snapshot_json, snapshot_sha256, overrides_json, status, demo,
  created_utc, started_utc, ended_utc, lease_owner, lease_until, heartbeat_utc, rev, reason, exit_code, attempt,
  cancel_requested, relaunch_of, events_sha256, evidence_json)
SELECT id, version_id, definition_id, snapshot_json, snapshot_sha256, overrides_json, status, demo,
  created_utc, started_utc, ended_utc, lease_owner, lease_until, heartbeat_utc, rev, reason, exit_code, attempt,
  cancel_requested, relaunch_of, events_sha256, evidence_json FROM runs ORDER BY rowid;
DROP TABLE runs;
ALTER TABLE runs_new RENAME TO runs;
CREATE INDEX runs_status_created ON runs(status, created_utc);
CREATE TRIGGER runs_snapshot_immutable BEFORE UPDATE OF snapshot_json, snapshot_sha256, overrides_json, version_id, demo ON runs
WHEN NEW.snapshot_json IS NOT OLD.snapshot_json OR NEW.snapshot_sha256 IS NOT OLD.snapshot_sha256
  OR NEW.overrides_json IS NOT OLD.overrides_json OR NEW.version_id IS NOT OLD.version_id OR NEW.demo IS NOT OLD.demo
BEGIN SELECT RAISE(ABORT, 'immutable'); END;
CREATE TRIGGER runs_terminal_final BEFORE UPDATE OF status ON runs
WHEN OLD.status IN ('succeeded','failed','cancelled') AND NEW.status IS NOT OLD.status
BEGIN SELECT RAISE(ABORT, 'terminal_state'); END;
CREATE TRIGGER runs_no_delete BEFORE DELETE ON runs
BEGIN SELECT RAISE(ABORT, 'immutable'); END;

CREATE TABLE stages_new (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  key TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('queued','running','succeeded','failed','cancelled','blocked',
    'skipped','interrupted')),
  attempt INTEGER NOT NULL CHECK (attempt BETWEEN 1 AND 8),
  started_utc TEXT,
  ended_utc TEXT,
  exit_code INTEGER,
  reason TEXT,
  rev INTEGER NOT NULL DEFAULT 0,
  idem_key TEXT,
  claimed_by TEXT,
  tokens INTEGER NOT NULL DEFAULT 0,
  cost_cents INTEGER NOT NULL DEFAULT 0,
  UNIQUE (run_id, key, attempt)
);
INSERT INTO stages_new(id, run_id, key, status, attempt, started_utc, ended_utc, exit_code, reason, rev)
SELECT id, run_id, key, status, attempt, started_utc, ended_utc, exit_code, reason, rev FROM stages ORDER BY rowid;
DROP TABLE stages;
ALTER TABLE stages_new RENAME TO stages;
CREATE UNIQUE INDEX stages_idem ON stages(idem_key) WHERE idem_key IS NOT NULL;

CREATE TABLE run_expansions (
  run_id TEXT NOT NULL REFERENCES runs(id),
  planner_key TEXT NOT NULL,
  stages_json TEXT NOT NULL,
  created_utc TEXT NOT NULL,
  PRIMARY KEY (run_id, planner_key)
);
CREATE TRIGGER run_expansions_no_update BEFORE UPDATE ON run_expansions
BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER run_expansions_no_delete BEFORE DELETE ON run_expansions
BEGIN SELECT RAISE(ABORT, 'append_only'); END;

CREATE TABLE run_audit (
  run_id TEXT NOT NULL REFERENCES runs(id),
  seq INTEGER NOT NULL CHECK (seq >= 1),
  utc TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  stage_key TEXT,
  detail_json TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL,
  PRIMARY KEY (run_id, seq)
);
CREATE TRIGGER run_audit_gapless BEFORE INSERT ON run_audit
WHEN NEW.seq IS NOT (SELECT COALESCE(MAX(seq), 0) + 1 FROM run_audit WHERE run_id = NEW.run_id)
BEGIN SELECT RAISE(ABORT, 'seq_gap'); END;
CREATE TRIGGER run_audit_no_update BEFORE UPDATE ON run_audit
BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER run_audit_no_delete BEFORE DELETE ON run_audit
BEGIN SELECT RAISE(ABORT, 'append_only'); END;
