-- AHS-06 / 005: AI-assisted drafts. Additive (one new table). A draft is a 'proposed' object, NOT a blueprint version:
-- it has no run, approval or version link until a human saves it (status 'saved', definition_id set).
-- Only the prompt hash is stored, never the prompt text.
CREATE TABLE assist_drafts (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind = 'ai_draft'),
  status TEXT NOT NULL CHECK (status IN ('proposed','saved')),
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_sha256 TEXT NOT NULL,
  created_utc TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  spec_json TEXT NOT NULL,
  spec_sha256 TEXT NOT NULL,
  review_json TEXT NOT NULL,
  review_sha256 TEXT NOT NULL,
  definition_id TEXT REFERENCES harness_definitions(id)
);
CREATE INDEX assist_drafts_spec ON assist_drafts(spec_sha256, status);
CREATE TRIGGER assist_drafts_content_immutable BEFORE UPDATE OF id, kind, provider, model, prompt_sha256, created_utc,
  name, description, spec_json, spec_sha256, review_json, review_sha256 ON assist_drafts
BEGIN SELECT RAISE(ABORT, 'immutable'); END;
CREATE TRIGGER assist_drafts_saved_final BEFORE UPDATE ON assist_drafts WHEN OLD.status = 'saved'
BEGIN SELECT RAISE(ABORT, 'saved'); END;
