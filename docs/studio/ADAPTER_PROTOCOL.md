# AHS-04 · Custom harness adapter protocol `aether.studio.adapter/v1`

An adapter connects an external harness (another agent runner, a job queue, a CI system) to Harness Studio. Studio
treats it as untrusted third-party code: it gets **no shell, no network, no host files and no secrets** by default, and
nothing it says can make Studio do any of those things on its behalf. CE only (`AETHER_MODE=custom`); live mode never
imports any of this (`test_live_does_not_import_runner`).

Code: `aether/studio/adapter_protocol.py` (codec, negotiation, chain), `aether/studio/adapter_sandbox.py` (launcher),
`aether/studio/executors/adapter.py` (`AdapterExecutor`), migration `003_adapters.sql`. Example:
`examples/adapters/echo_adapter.py`. Tests: `tests/studio/test_adapter_*.py`, fixtures `tests/studio/fixtures/`.

## 1. Transport and why

**JSON lines over the stdin/stdout pipes of a subprocess that Studio starts itself.** Each line is one UTF-8 JSON
object, at most **64 KiB** including the newline; duplicate keys, unknown message types and unknown fields are errors.

Considered: loopback HTTP with a per-adapter token. Rejected because it needs a listening socket (network privilege for
the adapter or for Studio), a token minted, stored, rotated and kept out of exports, and it cannot tie the adapter's
lifetime to the job. With stdio:

- **No credential exists.** Authentication is possession of the pipe that Studio created; there is nothing to store,
  leak or export.
- **The process is Studio's child** in its own process group: timeout, cancel and revoke end with `SIGKILL` of the
  group, and the exit is observed directly.
- **The channel needs no privilege**, so the sandbox (§3) can deny every socket outright.

Launch: `python3 -I -S -B adapter_sandbox.py SCRATCH SCRIPT ARGS…` with `shell=False`, `close_fds=True`,
`start_new_session=True`, cwd = a fresh `0700` scratch dir under `$AETHER_DATA_DIR/adapter-scratch/`, and this exact
environment: `PATH=/usr/bin:/bin LANG=LC_ALL=C.UTF-8 HOME=TMPDIR=<scratch> AETHER_ADAPTER_PROTOCOL=aether.studio.adapter/v1`.
`SCRIPT` and `ARGS` are fixed at enrollment (§5). Adapters are Python (stdlib only, since `-S` disables site-packages)
in v1. A native executable would need an `execve` after confinement, which a stdlib seccomp filter cannot allow exactly
once. One process per stage attempt, plus short health probes; the scratch dir is removed when the process exits.

## 2. Messages

Studio → adapter: `hello {protocol, session, offered}`, `welcome {session, capabilities}`, `refused {code}`,
`submit {job}`, `observe {ref}`, `cancel {grace_seconds}`, `ping {n}`, `bye`.

Adapter → Studio (closed set, data only):

| type | fields | when |
|---|---|---|
| `hello` | `protocol`, `name` (1..80), `capabilities` (list ≤ 8) | reply to Studio `hello` |
| `accepted` | `ref` (`[A-Za-z0-9._:-]{1,128}`, the external job id) | reply to `submit`/`observe` |
| `pong` | `n` (must be a sent, not yet answered ping) | reply to `ping` |
| `event` | `seq`, `prev`, `kind`, `data`, (`content` only for `artifact`) | after `accepted` |

`submit.job = {run_id, stage, attempt, task, timeout_seconds, inputs, outputs}`. `task` comes from the stage step
`submit:<task>`, and `observe:<ref>` attaches to an existing external job. Phases are strict:
`hello` → (`welcome`) → `pong`* → (`submit|observe`) → `accepted` → `event`*/`pong`*. A message outside its phase is
`unexpected_message`, and any event after a `result` is also rejected.

Event kinds and `data` (exact fields):

| kind | data |
|---|---|
| `log` | `{stream: stdout\|stderr, text ≤ 4000}` (secret shapes are redacted on ingest, see §4) |
| `progress` | `{done: 0..total, total: 1..1e9}` |
| `status` | `{state: accepted\|running\|waiting\|finishing}` |
| `artifact` | `{name ∈ stage outputs, media_type ∈ text/plain\|application/json\|text/markdown, sha256, size}` + top-level `content` (text) |
| `result` | `{exit_code: 0..255}`: the only way to finish a job |

## 3. Sandbox (`adapter_sandbox.py`, fail-closed)

Applied in the launcher before the adapter's first line runs; the script then runs in-process (`runpy`):

1. **rlimits:** no core dumps, 64 open files, 64 MiB file size, 1 GiB address space.
2. **Landlock** (unprivileged LSM). The filesystem is limited to read/execute on the system library dirs, the
   interpreter's stdlib and the script's own directory, read/write on the scratch dir, and `/dev/null`. The data dir
   (DB, `owner.token`), home, `/etc` and `/app` are unreadable. ABI ≥ 4 also denies TCP bind/connect; ABI ≥ 6 denies
   signals and abstract-unix sockets outside the sandbox.
3. **seccomp** (after `PR_SET_NO_NEW_PRIVS`): `EPERM` for `fork`, `vfork`, `clone` without `CLONE_THREAD`, `execve`,
   `execveat`, `socket`, `socketpair`, `io_uring_setup`, `ptrace`, `bpf`, `perf_event_open`, `userfaultfd`, `unshare`
   and `setns`. `clone3` gets `ENOSYS` so libc falls back to `clone`, and threads keep working. x32 syscalls are
   refused and a foreign arch is killed. Supported: x86_64 and aarch64.

If any step fails the launcher exits **70** with `sandbox_unavailable: <step>`. Health reports it, and the adapter never
runs unconfined. Verified on the host (kernel 6.12, Landlock ABI 6) and inside the hardened CE container
(`aether-ce:local`, `--cap-drop ALL --read-only no-new-privileges`, Docker default seccomp). The hostile fixture's
`escape` mode records every attempt (`test_adapter_sandbox.py`): environment = the 6 variables above; reading a planted
file, the data dir or `owner.token`, a shell, `os.system`, `fork`, `execv`, TCP to a live loopback listener, a unix
socket and signalling the parent are all **denied**; writing its own scratch dir is allowed.

Residual risk, stated plainly: the adapter can read world-readable system libraries, burn CPU until the stage timeout,
and use up to 1 GiB of address space (the CE container's `mem_limit` and `pids_limit` still apply).

## 4. Event integrity: sequence + SHA-256 hash chain

Per session Studio picks a random 128-bit `session` nonce and sends it in `hello`. The chain starts at
`h0 = sha256("aether.studio.adapter/v1|" + session)`. Event *n* must carry `seq = n` and `prev = h(n-1)`, and
`h(n) = sha256(canonical_json({seq, prev, kind, data}))` (sorted keys, `,`/`:` separators, UTF-8). Artifact `content` is
bound through `data.sha256`/`data.size`, which Studio recomputes. Studio verifies every link on ingest and stores each
accepted event as run event `adapter.event` with
`data = {source: "adapter", protocol_version, adapter, session, ref, kind, i: seq, prev, sha256: h(n), data, redacted}`.
These events are **non-DEMO** (`demo=false`) and always marked `source=adapter`. A session starts with run event
`adapter.session {source, protocol_version, adapter, adapter_id, session, chain_seed: h0, capabilities}`.

This gives tamper evidence for ordering, gaps, replay and content (integrity, not authorship). Authenticity comes from
the pipe; no signing key is needed or stored. `adapter_protocol.verify_stored(events)` re-verifies a stored run offline:
seed, contiguous `i`, `prev` continuity and recomputed hashes. When a log line was redacted on ingest (`redacted: true`),
its hash was verified at ingest and continuity is still checked, but it cannot be recomputed from the redacted text.
The run's `events_sha256` (INTERFACE_SPEC §6) then seals the whole event log at the terminal state.

**Rejections are never silent.** An oversized line, invalid JSON, a duplicate key, an unknown message or field, a bad
`seq`, a broken `prev`, a bad kind schema, an undeclared or mismatched artifact, more than 10,000 events, a bad `pong`,
or an event before `accepted` each produces run event `executor.error {code: "adapter_<code>"}`. The error is persisted
*before* the failure becomes visible to the runner. Studio then kills the process group and fails the stage with
`reason=executor_error`. Events accepted before the violation remain a valid chain.

## 5. Capability negotiation and least-privilege enrollment

Adapter scopes are `observe`, `submit` and `cancel`, mapped to Studio capabilities `adapter.<scope>` (INTERFACE_SPEC §7).
Effective scopes = **declared by the adapter ∩ granted by the owner ∩ available in this mode**. A protocol other than
`aether.studio.adapter/v1` is refused with `refused {code: version_mismatch}`. Health then reads
`adapter_version_mismatch: adapter speaks X, Studio speaks aether.studio.adapter/v1`, and the run is
`blocked(adapter_unavailable)`. An undeclared scope name (`shell`, `network`, …) is refused with `unknown_capability`.
`submit` and `cancel` are optional: an observe-only adapter is valid, and without `cancel` Studio cancels by signal (§6).

Enrollment (`adapters` table, owner-authenticated API):

| route | body | effect |
|---|---|---|
| `POST /api/studio/adapters` | `{name: Key, script: abs path, args: [≤16 strings]}` | new record, **`granted: []`** |
| `POST /api/studio/adapters/<id>/grant` | `{scopes: [...], confirm: "<id>"}` | replaces the grant set (explicit, confirmed) |
| `POST /api/studio/adapters/<id>/revoke` | `{confirm: "<id>"}` | one-way; grants cleared; live sessions killed |
| `POST /api/studio/adapters/<id>/probe` | `{}` | launch + handshake + ping now |
| `GET /api/studio/adapters[/<id>]` | | records + health |

Enrollment validation rejects the following:
- a script that is not a regular `.py` file, or that is group/world-writable or sits in such a directory;
- a script not owned by root or the Studio user;
- a script whose directory contains or lies inside the data dir (the sandbox grants read access to that directory);
- arguments containing control characters or matching the CE secret scanner or secret-field names
  (`adapter_secret_detected`, value never echoed).

The script's SHA-256 is pinned. If the file changes, the adapter becomes `unavailable` until it is re-enrolled. Launch
fields are immutable (DB triggers), rows are never deleted, and revocation cannot be undone. **No secret is ever
stored:** stdio adapters need none. Blueprints reference an adapter only by `executor.ref = <enrollment name>`, so
exports (`aether.studio.bundle/1`) carry no path, argument, id or hash of an enrollment (`test_blueprint_export_never_contains_enrollment`).

Blueprint rules (`schema.validate_spec`): an `executor.kind: adapter` spec needs `adapter.observe` in its capabilities and
`ref` must be a Key. Every stage step is `submit:<task>` (stage needs `adapter.submit`) or `observe:<ref>`.

## 6. Health, heartbeat, disconnect, cancel

- **`health(ref)`** returns `{status: ready|degraded|unavailable, observed_utc, detail, scopes}`. It re-probes
  (launch → handshake → ping/pong → bye) when the last contact is older than 30 s. **`health()`** without a ref
  aggregates cached contact only and never launches anything. Contact older than the TTL is reported `degraded`
  ("last contact Ns ago"), never `ready`. The UI reads it from `/api/studio/mode` (`executors.adapter`) and
  `/api/studio/capabilities` (`adapters`).
- **Runner pre-flight** blocks a run (nothing starts) with `adapter_unavailable` when the adapter is not enrolled,
  revoked, lacks the `observe` grant, has a changed script, fails its probe, or the sandbox is unavailable. It blocks
  with `capability_unavailable` when a step needs an un-negotiated scope.
- **Heartbeat:** while a stage runs, Studio pings every 2 s. Any valid message counts as contact. No message for 2
  heartbeats makes health `degraded`. No message for 15 s ends the stage as `failed(adapter_unavailable)` with
  `executor.error adapter_unresponsive`, and the group is killed.
- **Disconnect:** an exit or crash before `result` gives `failed(executor_error)` with
  `executor.error adapter_disconnected` ("adapter exited N before a result"). After `result` the adapter has 2 s to
  exit, then it is killed. The `result` exit code decides the outcome (0 succeeds, non-zero gives `exit_nonzero`).
- **Cancel** (owner cancel, stage/wall timeout): with the `cancel` scope Studio sends `cancel {grace_seconds}`, and
  without it the group gets `SIGTERM`. After `cancellation.grace_seconds`, Studio sends `SIGKILL` to the process group
  (and kills at once for grace 0). An exit during cancel is expected and is not recorded as an error.
- **Revoke:** the store revokes first, then the executor fails every live session of that adapter with
  `executor.error adapter_revoked` → `failed(adapter_unavailable)`. The error is written under the same lock that
  serializes ingest, so **no adapter event is stored after it** (`test_revoke_stops_events_and_fails_run`).
  Later runs are blocked.

## 7. Writing an adapter

Read `examples/adapters/echo_adapter.py` (about 100 lines of stdlib). Answer `hello` with your protocol, name and scopes,
answer every `ping`, reply `accepted` to `submit`/`observe`, keep the chain, and end with a `result` event. Read stdin
with `os.read` + your own buffer if you also use `select`; buffered `readline` hides queued lines from `select`.
