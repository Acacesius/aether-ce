# Studio workflow orchestration (AHS-05)

**Scope.** This is the CE Studio runner only (`AETHER_MODE=custom`, local 127.0.0.1, no host socket).
A live deployment keeps its own scheduler: its server never imports `aether.studio.*` beyond the mode switch, and
nothing here touches any external queue, schema or data.

Code: `aether/studio/orchestrator.py` (per-run `RunDriver`), `aether/studio/orchestration_store.py` (claims,
checkpoints, recovery, audit), `aether/studio/runner.py` (dispatcher: claims runs, one driver thread per run),
migration `004_orchestration.sql`.

## Execution model

* A run is a DAG of stages. A stage becomes **ready** when every stage in `needs` is `succeeded`.
* The driver is a single-threaded state machine (`step()`), driven by the runner's poll loop. All state is in
  SQLite, so a driver built on a re-claimed run continues from its checkpoint.
* **Roles** (`role`, default `worker`):
  * `planner` may emit **one** bounded list of follow-up stage proposals (executor event `stage.plan`
    `{"stages": [...]}`), applied only if the planner stage succeeds.
  * `worker` does the work.
  * `reviewer` receives, in `StageView.upstream`, each direct dependency's status, verdict and artifact refs
    (id, name, sha256, size, path) and must emit `stage.verdict` `{"verdict": "pass"|"fail", "reason": "..."}`.
    The verdict is recorded as a `review.verdict` event, a `verdict` artifact (JSON) and an audit entry.
    `fail` fails the stage (`review_rejected`, never retried), which fails the run if the stage is required.
    A missing or invalid verdict is a failure, never a pass.
* **Planner expansion rules** (`schema.validate_expansion`, same stage schema as the blueprint):
  at most `MAX_PROPOSALS` (16) per expansion; total stages <= `MAX_STAGES` (64); depth <= `MAX_DEPTH` (16);
  stage capabilities must be a subset of the blueprint's (no new or forbidden capabilities); same executor
  (no new executors); unknown fields rejected; new keys only (existing stages are never edited); every added
  stage must descend from the planner; **proposed stages cannot be planners** (no recursion, no agents spawning
  agents). Expansion happens at most once per planner stage (primary key on `run_expansions`), in the same
  transaction that ends the planner stage, before any added stage is claimed, so the stage count is fixed
  before added stages execute. A rejected proposal fails the planner stage (`expansion_rejected`) and writes a
  `plan.rejected` audit entry; nothing is added. Only planners may emit `stage.plan` (others get `executor.error`).
* **Fan-out lanes.** A stage's optional `max_workers` (1..4) caps how many of its direct dependants run at once
  (e.g. a planner fanning out workers). Everything is additionally bounded by `limits.max_workers`
  (per run) and the Runner's global stage cap (`Runner(max_stages=...)`, default `max_workers * 4`).

## Limits

| Limit | Where | Range | Behaviour |
|---|---|---|---|
| `limits.max_workers` | blueprint | 1..4 | concurrent stages per run |
| stage `max_workers` | stage (optional) | 1..4 | concurrent direct dependants of that stage |
| `Runner(max_workers)` | process | 1..4 | concurrent runs |
| `Runner(max_stages)` | process | >=1, default 4 x max_workers | concurrent stages across all runs |
| `limits.wall_seconds` | blueprint | 1..86400 | whole run, cumulative across stages, retries **and restarts** (checkpointed in `runs.wall_used`); exceeded -> `budget_exceeded` |
| `limits.tokens` | blueprint | 0..10 000 000 (0 = not enforced; must be 0 without a provider) | sum of executor `stage.usage` over all stages and attempts |
| `limits.cost_usd_cents` | blueprint | 0..100 000 (same rules) | same |
| stage `timeout_seconds` | stage | 1..86400 | one attempt; exceeded -> cancel with grace, stage `failed` (`timeout`), not retried |
| `retry_max` | stage | 0..3 | extra attempts after `exit_nonzero` |
| `retry.max_attempts` | blueprint | 1..3 | attempt **floor** for every stage: allowed attempts = `max(1 + retry_max, retry.max_attempts)` |
| `retry.backoff_seconds` | blueprint | 0..600 | wait before each retry attempt |
| `cancellation.grace_seconds` | blueprint | 0..60 | cancel-with-grace for timeout, cancel, budget, stop |
| `MAX_STAGES` / `MAX_DEPTH` / `MAX_PROPOSALS` | code | 64 / 16 / 16 | blueprint + planner expansions |

**Budgets.** When a budget is exceeded the driver stops scheduling, cancels running stages with grace
(stage `cancelled`, reason `budget_exceeded`), marks queued stages `skipped` (`budget_exceeded`) and ends the run
`failed` with reason `budget_exceeded` (a `limit.exceeded` event and a `budget.exceeded` audit entry say which).
The previous behaviour (wall limit -> reason `timeout`) is replaced; per-stage timeout still reports `timeout`.

**Retries** are new attempt rows (`stages` is one row per attempt, idempotency key `sha256(run:stage:attempt)`).
An attempt is claimed by compare-and-set `queued -> running` under the run lease; a retry is created only after
the previous attempt ended, so one attempt is never executed twice and attempts of a stage never overlap.

## Partial failure semantics

| Situation | Result |
|---|---|
| required stage fails (retries exhausted) | its dependants -> `skipped` (`upstream_failed`); independent branches continue |
| `on_failure: "stop"` (blueprint; default `continue_independent`) | no new stage starts, running stages get cancel-with-grace (`cancelled`, `stopped_on_failure`), queued -> `skipped`; run `failed` with the failing stage's reason |
| stage `optional: true` fails | does not fail the run; its dependants are still `skipped` |
| a **required** stage ends `skipped`/`cancelled` | run is `failed` (`dependency_failed`): never reported as success |
| all required stages `succeeded` (and declared evidence produced) | run `succeeded` |
| executor asks for an approval | stage `blocked`, run drains running siblings then `blocked` (`approval_pending`) |

Run status is `succeeded` only if every required stage succeeded. Stage statuses: `queued running succeeded
failed cancelled blocked skipped interrupted`.

## Resume, checkpoints, recovery

* Every stage attempt and every transition is committed in its own transaction together with the run event and
  audit entry that describe it. Expansions are persisted with the planner stage's end.
* `Runner.recover()` (at start and every tick) calls `Store.recover_expired_leases()` for runs whose lease expired:
  * stages that were `running` become `interrupted` (never silently "succeeded");
  * if **every** interrupted stage is `restartable: true` and was not interrupted before, the run returns to
    `queued` and is re-claimed; the stage re-runs **at most once** as a new attempt;
  * otherwise the run goes `blocked` with reason `owner_action_required` and nothing re-executes until the owner
    presses **Resume** (`POST /api/studio/runs/<id>/resume`), accepting that the stage may re-run;
  * if no stage was running the run simply resumes; `cancel_requested` runs are cancelled instead.
  * **Completed stages are never re-executed** (the driver reloads statuses from the database).
* **Approving a blocked run resumes it**: granting the last pending approval of a run blocked on
  `approval_pending` requeues it; the `blocked` stage gets a fresh attempt and the executor sees the grant in
  `StageView.approved`. Denial still fails the run (`approval_denied`). `POST .../resume` works for any other
  blocked run that has no pending approval.
* Duplicate claims: `claim_stage` is a compare-and-set on the attempt row under the run lease; a second claimant
  (another process, a race) gets `False` and the driver reports `lost` without executing.
* `runs.started_utc` keeps the first start; `runs.wall_used` carries elapsed wall seconds across restarts.

## Cancel

`POST /api/studio/runs/<id>/cancel`: no new stage starts; running stages get `executor.cancel(handle, grace)` and
are ended (hard stop after the grace period if the executor does not exit); queued stages become `cancelled`;
the run ends `cancelled` (`cancelled_by_owner`). A queued/blocked run is cancelled immediately.

## Audit log

`run_audit`: append-only (UPDATE/DELETE blocked by triggers, gapless `seq`), hash-chained per run:
`hash = sha256(canonical([prev_hash, run_id, seq, utc, actor, action, stage_key, detail]))`, root `0*64`.
Entries are written in the same transaction as the decision. Actions: `stage.scheduled/started/succeeded/failed/
cancelled/blocked/skipped/interrupted/retried/resumed`, `plan.expanded/rejected`, `review.verdict`,
`budget.exceeded`, `run.cancel/stop/budget` (halt), `run.cancel_requested`, `run.resumed/blocked/finished`,
`approval.granted/denied`. Actors: `runner`, `owner`, `planner:<stage>`, `reviewer:<stage>`.
Details are plain JSON; free text is truncated to 300 chars and secret-shaped text is redacted
(`scanner.redact`). The sealed run evidence records `audit: {entries, head}`.

`GET /api/studio/runs/<id>/audit` (same bearer auth as the other studio routes, read-only) returns
`{entries, verify: {ok, entries, broken_seq, head}}`; `Store.verify_audit` recomputes the chain and reports the
first entry that does not verify. Tampering that also rewrites the whole chain is caught by comparing `head`
with the sealed evidence. Table-level trigger removal needs direct database access and is only detectable
through the chain, not prevented.

## Executor events added

`stage.usage {tokens, cost_usd_cents}` (integers >= 0), `stage.plan {stages}` (planner), `stage.verdict
{verdict, reason}` (reviewer). Anything else unknown is still turned into `executor.error`.

## UI

Runs tab -> run detail: the stage graph lists every stage of the (expanded) DAG with status, role,
`attempt n/N`, reason, exit code and `needs`; an *Audit trail* disclosure shows the entries and whether the hash chain
verified; blocked runs without a pending approval show **Resume run**. No new navigation destination.

## Known gaps

* `retry.max_attempts` is a floor, not a cap, to keep existing blueprints' behaviour (default 1) unchanged.
* Backoff timers are not persisted: after a restart a retry attempt starts immediately.
* Token/cost enforcement needs an executor that reports `stage.usage`; the DEMO mock reports none (and
  tokens/cost must be 0 without a provider).
