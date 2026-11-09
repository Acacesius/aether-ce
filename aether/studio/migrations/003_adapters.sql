-- AHS-04 / 003: adapter enrollments. Launch fields are immutable; revocation is one-way; rows are never deleted.
-- No secret material exists for stdio adapters, so none is stored. Enrollments are never part of a blueprint export.
CREATE TABLE adapters (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  script TEXT NOT NULL,
  args_json TEXT NOT NULL,
  script_sha256 TEXT NOT NULL,
  granted_json TEXT NOT NULL DEFAULT '[]',
  created_utc TEXT NOT NULL,
  updated_utc TEXT NOT NULL,
  revoked_utc TEXT,
  rev INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX adapters_active_name ON adapters(name) WHERE revoked_utc IS NULL;
CREATE TRIGGER adapters_no_delete BEFORE DELETE ON adapters
BEGIN SELECT RAISE(ABORT, 'immutable'); END;
CREATE TRIGGER adapters_launch_immutable BEFORE UPDATE OF id, name, script, args_json, script_sha256, created_utc ON adapters
BEGIN SELECT RAISE(ABORT, 'immutable'); END;
CREATE TRIGGER adapters_revoke_once BEFORE UPDATE ON adapters WHEN OLD.revoked_utc IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'revoked'); END;
