# AHS-00 · Harness Studio interface specification (v1)

`schema_version: "aether.studio/v1"`. JSON over HTTP, UTF-8, `sort_keys` output. All objects reject unknown
fields and duplicate keys (same parser as `server.py:692-699`). Timestamps: RFC 3339 UTC with `Z`.
IDs: `<prefix>_<26-char Crockford ULID>` (`hd_`, `hv_`, `run_`, `stg_`, `apr_`, `art_`, `evt` uses integer seq).
Strings are NFC, no NUL, max lengths as stated. Applies to CE runner (AHS-01/02) and to adapters (AHS-04).

## 1. Common types

| Type | Definition |
|---|---|
| `Id` | `^(hd|hv|run|stg|apr|art)_[0-9A-HJKMNP-TV-Z]{26}$` |
| `Key` | `^[a-z][a-z0-9_-]{0,39}$` (stage keys, input names) |
| `Status` | enum `queued`, `running`, `succeeded`, `failed`, `cancelled`, `blocked` |
| `Duration` | integer seconds, 1..86400 |
| `Sha256` | `^[0-9a-f]{64}$` |
| `Limits` | `{wall_seconds: Duration (req), tokens: int 0..10_000_000 (0 = none allowed), cost_usd_cents: int 0..100000 (CE provider=none ⇒ must be 0), max_workers: int 1..4}` |

## 2. HarnessDefinition (mutable library entry)

```json
{
  "id": "hd_…", "schema_version": "aether.studio/v1",
  "name": "string 1..80", "description": "string 0..2000",
  "created_utc": "…", "updated_utc": "…", "archived": false,
  "rev": 3,                         // CAS counter, server-assigned
  "latest_version": 2               // server-assigned
}
```
Mutations: `PUT /api/studio/definitions/<id>` with `If-Match: <rev>` → 409 `rev_conflict` if stale. Archive
hides, never deletes. Content changes create a new HarnessVersion; the definition holds metadata only.

## 3. HarnessVersion (immutable)

```json
{
  "id": "hv_…", "definition_id": "hd_…", "version": 2,
  "schema_version": "aether.studio/v1", "created_utc": "…",
  "spec_sha256": "<sha256 of canonical spec>",
  "spec": {
    "mission": "string 1..4000",
    "workspace": {"kind": "scratch" | "mounted", "mount": "workspace-id or null", "access": "ro" | "rw"},
    "inputs": [{"name": "Key", "type": "string|integer|boolean|enum", "required": true,
                "default": null, "enum": [], "max_length": 4000, "secret": false}],
    "executor": {"kind": "mock" | "adapter", "ref": "mock:success | adapter-id", "demo": true},
    "capabilities": ["cap.id", "…"],           // must be ⊆ allowlist (§7)
    "network_allowlist": ["host:port"],         // requires cap net.egress.allowlist; CE default []
    "limits": { "...Limits" },
    "stages": [ { "...StageSpec" } ],           // 1..64, acyclic
    "retry": {"max_attempts": 1..3, "backoff_seconds": 0..600},
    "cancellation": {"grace_seconds": 0..60},
    "approvals": [{"stage": "Key", "capability": "cap.id", "reason": "string"}],
    "evidence": {"outputs": ["Key"], "require_tests": false}
  }
}
```
`secret: true` inputs are **forbidden** in v1 (validation error `secret_inputs_unsupported`); credentials belong to
adapters (§8). `spec_sha256 = sha256(canonical_json(spec))` where canonical = `json.dumps(sort_keys=True,
separators=(',',':'), ensure_ascii=False)`. Rows are immutable (DB trigger, and the API has no update route).

### StageSpec
`{key: Key, title: string 1..120, needs: [Key] (deps), executor_step: string 1..200 (opaque to runner, e.g. "mock:sleep:2"),
timeout_seconds: Duration, retry_max: 0..3, capabilities: [cap.id] ⊆ spec.capabilities, outputs: [Key]}`.
Validation: unique keys; every `needs` exists; no cycles (Kahn); depth ≤ 16.

## 4. Run

```json
{
  "id": "run_…", "version_id": "hv_…", "definition_id": "hd_…",
  "status": "queued", "demo": true, "reason": null,
  "snapshot": { "...frozen HarnessVersion.spec with overrides applied" },
  "snapshot_sha256": "…", "overrides": {"<input>": "value"},
  "created_utc": "…", "started_utc": null, "ended_utc": null, "heartbeat_utc": null,
  "exit_code": null, "attempt": 1, "rev": 0,
  "evidence": {"events_sha256": null, "artifacts": []}
}
```
- `demo` is true iff executor kind `mock`; the UI must label it **DEMO**; demo events carry `"demo": true`.
- Overrides may set only declared `inputs` (and lower, never raise, `limits`). Anything else → `override_not_allowed`.
- `snapshot`/`snapshot_sha256`/`overrides` are immutable after insert (trigger). Re-validating `snapshot` with the
  version's schema must reproduce `snapshot_sha256` (reproducibility test).
- `reason` enum when terminal or blocked: `timeout`, `cancelled_by_owner`, `executor_error`, `exit_nonzero`,
  `lease_lost`, `approval_denied`, `approval_pending`, `capability_unavailable`, `adapter_unavailable`,
  `dependency_failed`, `limit_exceeded`, `invalid_snapshot`.

### Run state machine

| From | To | Trigger | Guard |
|---|---|---|---|
| — | `queued` | submit | spec valid, capabilities approved, idempotency key new |
| `queued` | `running` | scheduler claims lease | `running_count < max_workers`; CAS on `rev` |
| `queued` | `blocked` | pre-flight | approval pending / capability or adapter unavailable |
| `queued` | `cancelled` | cancel | — |
| `blocked` | `queued` | approval granted / dependency recovered | CAS |
| `blocked` | `cancelled` | cancel | — |
| `blocked` | `failed` | approval denied/expired (`reason=approval_denied`) or dependency failed | — |
| `running` | `succeeded` | all stages succeeded, executor exit 0 **and** required evidence present | — |
| `running` | `failed` | stage failed after retries, timeout, lease lost on recovery, limit exceeded | — |
| `running` | `cancelled` | cancel; executor acknowledges or grace expires | — |
| `running` | `blocked` | stage hits pending approval | — |
| terminal (`succeeded`,`failed`,`cancelled`) | — | none | 409 `terminal_state` |

Recovery on start: `running` with `lease_until < now` → `failed` (`reason=lease_lost`); never silently re-executed.
Relaunch = new Run referencing the same version (`relaunch_of: run_…`), optionally new overrides.

## 5. Stage, Approval, Artifact

**Stage** `{id, run_id, key, status: Status, attempt: 1..4, started_utc, ended_utc, exit_code, reason}`.
Stage transitions mirror the run table; a stage is `queued` until all `needs` are `succeeded`; a failed need makes
dependents `blocked` with `reason=dependency_failed` (they never run). Run status is derived: any `running` → running;
all succeeded → succeeded; any failed (after retry) → failed; else blocked/queued.

**Approval** `{id, run_id, stage_key, capability, status: pending|granted|denied|expired, requested_utc,
decided_utc, decided_by: "owner", expires_utc}`. Decided once (CAS `pending → granted|denied`); expiry 24 h →
`expired` ⇒ stage `blocked`. `POST /api/studio/approvals/<id>` body `{"decision":"grant"|"deny","confirm":"<run id>"}`.
AI-drafted specs (AHS-06) can never create a granted approval.

**Artifact** `{id, run_id, stage_key, name: Key, media_type ∈ {text/plain, application/json, text/markdown, image/png},
size ≤ 16 MiB, sha256, created_utc, demo}`. Content-addressed, insert-only; fetched via
`GET /api/studio/artifacts/<id>` with `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`.
Artifacts are scanned with CE scanner rules for secret shapes on write; matches are stored redacted and flagged.

## 6. Events

`run_events` row / API object: `{run_id, seq (1.., gapless per run), utc, type, stage_key|null, demo, data}`.
`GET /api/studio/runs/<id>/events?cursor=<seq>&limit=1..200` → `{events, next_cursor, has_more, poll_after_ms}`.

| `type` | `data` |
|---|---|
| `run.submitted` | `{version_id, snapshot_sha256, idempotency_key_sha256}` |
| `run.status` | `{from, to, reason}` |
| `run.heartbeat` | `{lease_until}` (coalesced: at most 1 per 30 s stored) |
| `stage.status` | `{from, to, attempt, reason, exit_code}` |
| `stage.log` | `{stream: stdout|stderr, text ≤ 4000 chars, redacted: bool}` |
| `approval.requested` / `approval.decided` | `{approval_id, capability, decision}` |
| `artifact.created` | `{artifact_id, name, sha256, size}` |
| `limit.exceeded` | `{limit, value}` |
| `executor.error` | `{code, message ≤ 300}` |
| `adapter.session` | `{source:"adapter", protocol_version, adapter, adapter_id, session, chain_seed, capabilities}` (AHS-04) |
| `adapter.event` | `{source:"adapter", protocol_version, adapter, session, ref, kind, i, prev, sha256, data, redacted}`: hash-chained, verified on ingest (ADAPTER_PROTOCOL.md §4) |

Events are append-only; `events_sha256` = sha256 over canonical events in seq order at terminal state.

## 7. Capability allowlist

Every capability is `{id, description, risk: low|medium|high, ce_available: bool, requires_approval: bool}`.
Specs referencing unknown or unavailable capabilities fail validation (`capability_not_allowed`).

| id | CE | Approval | Meaning |
|---|---|---|---|
| `demo.run` | yes | no | mock executor steps |
| `workspace.read` | yes | no | read the mounted workspace |
| `workspace.write` | yes | **yes** | write the mounted workspace |
| `artifact.write` | yes | no | emit artifacts |
| `net.egress.allowlist` | no (v1) | yes | outbound to `network_allowlist` only |
| `provider.local` | only if `AETHER_PROVIDER=local` | yes | call a user-configured local model endpoint |
| `adapter.submit` / `adapter.observe` / `adapter.cancel` | yes (AHS-04; usable only through an enrolled adapter's explicit grant) | submit/cancel yes | custom harness adapter |

Never defined (rejected by name): `shell.host`, `ssh.*`, `wake.*`, `docker.socket`, `systemd.*`, `tmux.*`,
`provider.cloud`. No capability grants arbitrary host command execution.

## 8. Executor interface

```python
class Executor(Protocol):
    kind: str                       # 'mock' | 'adapter'
    demo: bool                      # True for mock
    def capabilities(self) -> frozenset[str]
    def health(self) -> dict        # {"status": "ready"|"unavailable", "observed_utc": str, "detail": str}
    def start(self, run: RunView, stage: StageView, emit: Callable[[str, dict], None]) -> Handle
    def poll(self, handle: Handle) -> dict   # {"state": "running"|"exited", "exit_code": int|None}
    def cancel(self, handle: Handle, grace_seconds: int) -> None
```
- `emit(type, data)` is the only output path; executors never touch the DB.
- Exceptions map to `executor.error` + stage `failed` (`reason=executor_error`); unknown outcome → `failed`,
  never `succeeded`.
- **MockExecutor** steps (`executor_step`): `mock:success`, `mock:fail:<code>`, `mock:sleep:<s>`, `mock:hang`,
  `mock:artifact:<name>`, `mock:approval:<cap>`. Deterministic; no I/O except artifact writes; every event `demo=true`.
- **AdapterExecutor** (AHS-04, as built; supersedes the token-file sketch): protocol `aether.studio.adapter/v1`,
  stdio JSON lines to a sandboxed subprocess, so no token exists or is stored. Scopes ⊆ `{observe, submit, cancel}`,
  granted explicitly per enrollment (default none). `health(ref)` probes the adapter, and the runner pre-flight calls
  `preflight(snapshot)`. `poll` may return `reason` (one of the run reasons) with `exit_code: null`. Full contract:
  `ADAPTER_PROTOCOL.md`.

## 9. Snapshot immutability

1. HarnessVersion rows: no UPDATE/DELETE (triggers `RAISE(ABORT,'immutable')`).
2. `runs.snapshot_json`, `snapshot_sha256`, `overrides_json`, `version_id`: UPDATE trigger aborts if changed.
3. The runner executes only `runs.snapshot_json`, never the current definition.
4. Schema upgrades (v1→v2) write a new derived column/row; the original snapshot bytes are preserved.

## 10. Export / import

Bundle: `{"format":"aether.studio.bundle/1","exported_utc","definitions":[{definition, versions:[…]}],"sha256"}`.
Excluded always: runs, events, artifacts, approvals, adapter tokens, owner token, absolute host paths
(workspace `mount` exported as the symbolic workspace id only), any field named `*token*|*secret*|*password*|*key*`.
Import: validates schema, rejects any value matching CE scanner secret rules (`import_secret_detected`), rejects
unknown capabilities, assigns **new** ids, preserves `spec_sha256` (verified), sets `archived=false`. Re-import of the
same bundle is idempotent by `(name, spec_sha256)`.

## 11. Error model

HTTP JSON `{"error": "<code>", "detail": "≤300 chars, no secrets", "field": "json.pointer|null"}`.

| HTTP | codes |
|---|---|
| 400 | `invalid_request`, `unknown_field`, `duplicate_field`, `schema_invalid`, `cycle_detected`, `override_not_allowed`, `secret_inputs_unsupported`, `import_secret_detected`, `adapter_secret_detected` |
| 401 | `owner_auth_required` |
| 403 | `invalid_host`, `invalid_origin`, `cross_site_request`, `capability_not_allowed`, `approval_required` |
| 404 | `not_found` |
| 409 | `rev_conflict`, `terminal_state`, `idempotency_conflict` (same key, different body), `mode_unavailable` |
| 413 / 415 | `body_too_large` (256 KiB) / `json_required` |
| 429 | `queue_full` (CE: >100 queued runs) |
| 503 | `private_storage_unavailable`, `executor_unavailable`, `adapter_unavailable` |

Existing AETHER codes (`server.py:709-715`) keep their meaning; Studio adds only the codes above.
