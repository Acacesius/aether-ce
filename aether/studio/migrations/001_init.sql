-- AHS-01 / 001: Studio CE schema (INTEGRATION_DESIGN §4). Applied once inside BEGIN IMMEDIATE.
CREATE TABLE schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_utc TEXT NOT NULL
);

CREATE TABLE harness_definitions (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  created_utc TEXT NOT NULL,
  updated_utc TEXT NOT NULL,
  archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
  rev INTEGER NOT NULL DEFAULT 0,
  latest_version INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE harness_versions (
  id TEXT PRIMARY KEY,
  definition_id TEXT NOT NULL REFERENCES harness_definitions(id),
  version INTEGER NOT NULL CHECK (version >= 1),
  schema_version TEXT NOT NULL,
  spec_json TEXT NOT NULL,
  spec_sha256 TEXT NOT NULL,
  created_utc TEXT NOT NULL,
  UNIQUE (definition_id, version)
);
CREATE INDEX harness_versions_sha ON harness_versions(spec_sha256);
CREATE TRIGGER harness_versions_no_update BEFORE UPDATE ON harness_versions
BEGIN SELECT RAISE(ABORT, 'immutable'); END;
CREATE TRIGGER harness_versions_no_delete BEFORE DELETE ON harness_versions
BEGIN SELECT RAISE(ABORT, 'immutable'); END;

CREATE TABLE runs (
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
    'dependency_failed','limit_exceeded','invalid_snapshot')),
  exit_code INTEGER,
  attempt INTEGER NOT NULL DEFAULT 1,
  cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
  relaunch_of TEXT REFERENCES runs(id),
  events_sha256 TEXT,
  evidence_json TEXT
);
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

CREATE TABLE stages (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  key TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('queued','running','succeeded','failed','cancelled','blocked')),
  attempt INTEGER NOT NULL CHECK (attempt BETWEEN 1 AND 4),
  started_utc TEXT,
  ended_utc TEXT,
  exit_code INTEGER,
  reason TEXT,
  rev INTEGER NOT NULL DEFAULT 0,
  UNIQUE (run_id, key, attempt)
);

CREATE TABLE run_events (
  run_id TEXT NOT NULL REFERENCES runs(id),
  seq INTEGER NOT NULL CHECK (seq >= 1),
  utc TEXT NOT NULL,
  type TEXT NOT NULL,
  stage_key TEXT,
  demo INTEGER NOT NULL CHECK (demo IN (0, 1)),
  data_json TEXT NOT NULL,
  PRIMARY KEY (run_id, seq)
);
CREATE TRIGGER run_events_gapless BEFORE INSERT ON run_events
WHEN NEW.seq IS NOT (SELECT COALESCE(MAX(seq), 0) + 1 FROM run_events WHERE run_id = NEW.run_id)
BEGIN SELECT RAISE(ABORT, 'seq_gap'); END;
CREATE TRIGGER run_events_no_update BEFORE UPDATE ON run_events
BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER run_events_no_delete BEFORE DELETE ON run_events
BEGIN SELECT RAISE(ABORT, 'append_only'); END;

CREATE TABLE approvals (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  stage_key TEXT NOT NULL,
  capability TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','granted','denied','expired')),
  requested_utc TEXT NOT NULL,
  decided_utc TEXT,
  decided_by TEXT CHECK (decided_by IS NULL OR decided_by = 'owner'),
  expires_utc TEXT NOT NULL
);
CREATE TRIGGER approvals_decided_once BEFORE UPDATE OF status ON approvals
WHEN OLD.status IS NOT 'pending'
BEGIN SELECT RAISE(ABORT, 'decided'); END;

CREATE TABLE artifacts (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  stage_key TEXT NOT NULL,
  name TEXT NOT NULL,
  media_type TEXT NOT NULL CHECK (media_type IN ('text/plain','application/json','text/markdown','image/png')),
  size INTEGER NOT NULL CHECK (size BETWEEN 0 AND 16777216),
  sha256 TEXT NOT NULL,
  path TEXT NOT NULL,
  demo INTEGER NOT NULL CHECK (demo IN (0, 1)),
  created_utc TEXT NOT NULL
);
CREATE TRIGGER artifacts_no_update BEFORE UPDATE ON artifacts
BEGIN SELECT RAISE(ABORT, 'insert_only'); END;
CREATE TRIGGER artifacts_no_delete BEFORE DELETE ON artifacts
BEGIN SELECT RAISE(ABORT, 'insert_only'); END;

CREATE TABLE submissions (
  idempotency_key TEXT PRIMARY KEY,   -- sha256 of the client key
  request_sha256 TEXT NOT NULL,
  run_id TEXT NOT NULL REFERENCES runs(id),
  created_utc TEXT NOT NULL
);
CREATE TRIGGER submissions_no_update BEFORE UPDATE ON submissions
BEGIN SELECT RAISE(ABORT, 'insert_only'); END;
