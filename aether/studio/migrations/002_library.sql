-- AHS-02 / 002: artifact secret-scan flag; upgraded (derived) run snapshots stored apart from the original.
ALTER TABLE artifacts ADD COLUMN flagged INTEGER NOT NULL DEFAULT 0 CHECK (flagged IN (0, 1));

CREATE TABLE run_snapshot_upgrades (
  run_id TEXT PRIMARY KEY REFERENCES runs(id),
  from_schema TEXT NOT NULL,
  to_schema TEXT NOT NULL,
  upgraded_json TEXT NOT NULL,
  upgraded_sha256 TEXT NOT NULL,
  created_utc TEXT NOT NULL
);
CREATE TRIGGER run_snapshot_upgrades_no_update BEFORE UPDATE ON run_snapshot_upgrades
BEGIN SELECT RAISE(ABORT, 'immutable'); END;
CREATE TRIGGER run_snapshot_upgrades_no_delete BEFORE DELETE ON run_snapshot_upgrades
BEGIN SELECT RAISE(ABORT, 'immutable'); END;
