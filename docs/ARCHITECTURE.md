# Architecture (short)

One Python process (stdlib only) in one container.

```
browser UI (static, same origin) / curl / script ──HTTP 127.0.0.1──> app.py (guards: Host, Origin, Sec-Fetch-Site, bearer token)
                                      │
                                   api.py  (route table, validation, idempotency, If-Match)
                                      │
              ┌───────────────────────┼─────────────────────────┐
           store.py (SQLite, WAL)   runner.py (workers, lease)   exportimport.py + scanner.py
           definitions, versions,   claim FIFO, heartbeat 5 s,   bundle hash, secret-shape refusal,
           runs (frozen snapshot),  lease 30 s, timeouts,        artifact redaction
           events, approvals,       cancel, evidence seal
           artifacts metadata              │
                                   executors/mock.py (DEMO)   executors/adapter.py ──stdio──> sandboxed adapter
           orchestrator.py + orchestration_store.py: stage DAG, budgets, retries, resume, hash-chained audit
           assist/ (optional, off): loopback model -> strict JSON -> clamp -> review -> draft (never runnable)
```
- **UI** (`aether/studio/ui/`): static files loaded once at start, strict CSP, owner token kept in `sessionStorage` only.
- **Orchestration** (`docs/studio/ORCHESTRATION.md`): one driver per run, state in SQLite, completed stages never re-run.
- **Adapters** (`docs/studio/ADAPTER_PROTOCOL.md`): stdio JSON lines to a Landlock + seccomp sandboxed child; opt-in.
- **AI assist** (`docs/studio/AI_ASSIST.md`): loopback-only, optional, off by default.
- **Blueprints**: a definition has immutable versions (new version only when the spec hash changes; compare-and-swap on `rev`).
- **Runs** freeze a snapshot of the spec at submission (database triggers forbid change). Overrides are limited to declared inputs.
- **Capabilities** come from a fixed allowlist (`capabilities.py`); anything granting host execution is not defined.
- **Events** are an append-only sequence per run (cursor-based reads); at a terminal state the run is sealed with an events hash.
- **Mode switch** (`mode.py`) fails closed: CE needs `AETHER_MODE=custom`, `AETHER_RUNTIME=docker`, `AETHER_PROVIDER=none|local`,
  an absolute data directory, and refuses to start if a Docker socket is present or private-integration variables are set.
- **Storage**: everything is under `/data` (one named volume): `owner.token` (mode 0600), `studio.sqlite3`, `artifacts/`.
