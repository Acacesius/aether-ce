# Known limitations

CE is an alpha. These are known, deliberate or deferred; none is hidden behaviour.

## Product scope
- **The mock executor is DEMO only** (the CLI executor, below, is the real-work path in plain-process mode). The mock executor simulates steps (`mock:success`, `mock:fail:<code>`,
  `mock:sleep:<s>`, `mock:hang`, `mock:artifact:<name>`, `mock:approval:<capability>`, `mock:verdict:pass|fail`). No real
  work is performed. It cannot propose follow-up stages, so a *planner* stage never expands under the mock, and the
  reviewer verdict in `examples/demo-harness-dag.json` is scripted, not a judgement. Real planners and reviewers need an
  adapter.
- **A web UI exists** (Quick Run, Builder, Library, Runs) and is served by the same process. It is plain HTML/JS with no
  build step. It is covered by the author's automated tests (no browser matrix): there is no cross-browser testing and no
  external accessibility review.
- **Adapters are opt-in and Python-only.** Protocol `aether.studio.adapter/v1` is implemented (stdio JSON lines, hash-chained
  events, least-privilege enrolment, sandbox). An adapter is a stdlib-only Python script. The shipped Docker image does
  not contain one and the container cannot see host files, so using one in Docker needs a read-only script mount you add
  yourself. The sandbox was verified by the author inside the hardened container (see `docs/studio/ADAPTER_PROTOCOL.md` §3), not by
  a fresh-clone tester.
- **No artifact download endpoint.** The API and UI show artifact metadata and SHA-256; content is read from the data volume
  (`docker compose exec studio cat /data/artifacts/<run>/<sha256>`).
- **Single owner, single token.** No users, roles, or token rotation command (delete the volume to reset).
- **No TLS.** Loopback HTTP only; do not put CE behind a reverse proxy to expose it.

## Platforms and the CLI executor
- **Windows is untested by the author** (no Windows machine). Windows support (`start.bat`, `start.ps1`, process-tree kill via
  `taskkill`, `.cmd` shim handling, `icacls` permissions) is verified by code review, mocks and Linux tests only.
  macOS is also untested. Plain-process mode is the primary path; Docker is secondary and Linux-tested only.
- **File protection on Windows** (`aether/studio/fsperm.py`): the `icacls` result is parsed and verified only against
  mocked output, never real output. If it cannot be confirmed, Studio refuses to start rather than run with an
  unprotected owner token.
- **The CLI executor depends on the installed CLIs.** Their flags, output formats, login behaviour and usage-limit wording
  can change; a changed message may not be recognised as a limit (it then shows as a failure), and reset-time parsing
  falls back to a 5-hour block. You must have signed in with each CLI yourself; CE never stores credentials. Tested with
  fake CLI scripts only, never with the real `claude` / `codex`.
- **The CLI's edits are real.** It is restricted only by its own permission system and the flags CE passes; use a dedicated
  workspace folder.
- **Loopback only.** Studio refuses any host other than `127.0.0.1`/`localhost`; there is no remote or LAN mode and no TLS.
- **The adapter sandbox is Linux only** (Landlock + seccomp, x86_64 and aarch64, kernel with Landlock) and is disabled
  elsewhere, including Windows and macOS: adapters there do not run (exit 70, `sandbox_unavailable`); there is no unconfined
  fallback. The CLI executor does not use this sandbox. Docker Desktop's Linux VM is untested for Landlock.
- The sandbox tests assume such a kernel and fail (they are not skipped) on one without it.
- **Reporting back:** run `python -m aether.studio doctor` and follow [docs/FEEDBACK.md](docs/FEEDBACK.md) with the template
  `feedback/TESTER_FEEDBACK.md`. Windows steps: [docs/WINDOWS.md](docs/WINDOWS.md).

## Approvals and orchestration
- **Granting the last pending approval now resumes the blocked run** from its checkpoint; denying fails it
  (`approval_denied`). Approval expiry is checked lazily, only when a decision is attempted (no background sweeper).
- Runs interrupted by a restart: a stage marked `restartable` is re-run at most once; otherwise the run is `blocked` until you
  press **Resume**, accepting that the interrupted stage may run again. Completed stages never re-run.
- `retry.max_attempts` is a floor, not a cap. Backoff timers are not persisted: after a restart a retry starts at once.
- Token and cost budgets are enforced only for executors that report usage; the mock reports none and token/cost limits
  must be 0 without a provider.
- Default is 1 concurrent run (max 4); stages per run are bounded (64 stages, depth 16, planner proposals 16).

## AI assist
- **Off by default and optional.** It talks only to a model on a loopback address and never to a cloud service. Inside the
  default container, loopback is the container itself, so a model running on the host is refused by design; enabling assist
  means running a model in the container's own network namespace and adding the two `AETHER_STUDIO_ASSIST_*` variables
  to `compose.yaml` yourself. This path was tested with a fake loopback model only: no real local model has been
  qualified, and draft quality is unmeasured.
- A draft is never runnable until you review and save it; once you edit the form it is an ordinary human-authored spec.
- The review text is generated from the validated spec (permissions and risk), not from the model, and shows permissions,
  not intent.

## Blueprints and schema
- Schema v2 is a minimal bump (adds `tags`); newer optional fields (roles, `on_failure`, `max_workers`, `optional`,
  `restartable`) do not change the hash of specs that do not use them.
- Inline (`spec`) run submissions share the definition table by name; names are not unique, and an inline submission
  can bump `rev` of a same-named library entry.
- Import of an unchanged bundle is idempotent (nothing created). To import a modified bundle its `sha256` must be recomputed.

## Operations
- Restart recovery marks runs whose lease expired as interrupted/blocked/failed per the rules above; it never silently re-runs.
- The container has the default Docker bridge network. CE code makes no outbound connections, but CE does not firewall
  the container; Docker's own network policy applies.
- Image tag is `python:3.13-slim` (not digest-pinned); builds are reproducible only up to that base image.
- Some source comments cite design documents that are not part of this repository.

## Release status
- Licence: MIT (see `LICENSE`). The Studio UI ships no fonts (system font stack only).
- Tested on one Linux machine by the author only. **Windows: untested. macOS: untested. No external tester has
  run CE.**
- Release scanner: the owner private-literal denylist pass is **pending an owner-provided file** (`AETHER_CE_DENYLIST`).
