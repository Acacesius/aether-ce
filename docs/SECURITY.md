# Security model

## Assets and assumptions
Assets: the owner token, the data volume (definitions, runs, artifacts), and the host. CE assumes one trusted owner on a
machine they control. It is **not** hardened for multi-user or hostile networks.

## Threat model
| Threat | CE control |
|---|---|
| Remote network access | published only on `127.0.0.1`; `Host` must be `127.0.0.1:<port>` or `localhost:<port>` (anything else `403`) |
| Browser-based cross-site request (CSRF / DNS rebinding) | exact `Host`, `Origin` required on writes and must match, `Sec-Fetch-Site` must be same-origin/none, strict CSP, no CORS |
| Unauthenticated use | bearer token on every `/api/studio/*` call (constant-time compare); token generated in the volume, mode 0600, never logged |
| Container escape / host takeover | non-root uid 10001, read-only root fs, `cap_drop: ALL`, `no-new-privileges`, not privileged, no host network, no Docker socket, only one volume, pids/memory limits |
| Secrets in artifacts, inputs or imports | secret-shaped text is redacted/flagged in artifacts; secret-shaped images refused; `secret` inputs unsupported; imports with secret-shaped values rejected (matched text never echoed) |
| Malicious blueprint | closed schema (unknown fields rejected), capability allowlist, bounded stages/limits/timeouts, executor is a simulator |
| Malicious or buggy adapter | opt-in only; enrolled with no scopes; stdio pipe is the only channel (no token exists); runs in a Landlock + seccomp sandbox with no network, no process creation, no data-dir or home access; events are size-capped, schema-checked and hash-chained; ends by `SIGKILL` of its process group; never runs unconfined (`docs/studio/ADAPTER_PROTOCOL.md`) |
| Prompt injection / over-broad AI draft | assist is off by default; model output is strict JSON, clamped to a ceiling that only the user can raise, re-validated, shown with a code-generated review, and never runnable until saved with an acknowledgement; approvals can be added, never removed (`docs/studio/AI_ASSIST.md`) |
| User text leaving the machine through assist | only loopback `http://` endpoints (checked at connect time, redirects refused); no cloud provider capability exists; no key or token is read or sent |
| Real AI CLI run by the opt-in CLI executor (`AETHER_EXEC=cli`) | off by default; owner-chosen validated workspace (never the data dir); Claude gets file tools only, no shell/network/sub-agent tools; Codex runs `workspace-write` with network off; minimal environment; no `git push` path (see "CLI executor mode") |
| Credential exposure through slots, queue or logs | slots hold only a config-folder path (credential-shaped fields and values refused); prompts with secret-shaped text refused; served log text and events redacted; log, prompt and slots files owner-only |
| Run history tampering | per-run append-only, hash-chained audit log, verifiable over the API and recorded in the sealed evidence (`docs/studio/ORCHESTRATION.md`) |
| Accidental data leak in the release | `tools/ce_scan.py`, fresh single-commit history, allowlist export |

## What CE refuses
- Any `Host`/`Origin` other than the local ones; requests without a valid token.
- Starting with a Docker socket present, with private-integration environment variables set, with a relative or per-user-private data directory, or outside the `custom`/`docker` mode.
- Adapter enrolment of scripts that are writable by others, owned by other users, or inside the data directory; adapter arguments that look like secrets.
- Assist endpoints that are not loopback `http://`, carry credentials, or redirect; draft text that looks like a credential.
- Capabilities for host shell, remote login, wake, Docker socket, system service control, terminal multiplexers, cloud model providers.
- Spec fields it does not know; overrides for inputs that were not declared; cost/token limits when no provider is configured.
- Binding a non-loopback address unless running inside Docker (where the published port is still loopback-only).
- With `AETHER_EXEC=cli`: workspaces that are not absolute, missing, a filesystem root, or the data dir / inside / containing it; slot fields that could hold credentials; prompts that look like they contain a secret.

## What CE does not protect against
- Anyone who can already run Docker as you (they can read the volume and the token).
- A malicious local process able to reach `127.0.0.1:<port>` **and** read the token; the token is the only credential.
- Outbound traffic from the container: CE code makes none, but the default Docker bridge network is not firewalled by CE.
- Supply chain of the base image (`python:3.13-slim`, tag not digest-pinned).

## Residual risk of adapters
A sandboxed adapter can still read world-readable system libraries, use CPU until its stage timeout, and up to 1 GiB of address
space (the container's memory and process limits still apply). The sandbox is Linux only and was verified on one kernel family.
Treat an adapter you did not write as untrusted code even so, and grant it only the scopes it needs.

## CLI executor mode (opt-in)
Set `AETHER_EXEC=cli` and Studio can run the real `claude` or `codex` CLI, under the owner's own logins, as a subprocess in a
folder the owner picks (`docs/studio/CLI_EXECUTOR.md`). Unset (or any other value), none of this exists: no executor, no
queue, no routes, no process. What the opt-in grants: the CLI can **edit files** in the workspace, and it runs as your
account. This is not a demo executor.

- **No OS sandbox from Studio.** The Landlock/seccomp adapter sandbox is **not** applied to CLI runs. On Windows and macOS
  there is no OS-level confinement at all; on Linux the adapter sandbox is still not used. Only the CLI's own permission system
  and the flags below limit it.
- **Claude** is started with `--permission-mode acceptEdits`, `--allowedTools Read,Edit,MultiEdit,Write,Glob,Grep,LS,NotebookEdit,TodoWrite`
  and `--disallowedTools Bash,BashOutput,KillShell,WebFetch,WebSearch,Task`. No shell, no web tools, no sub-agents; so no
  `git push`, `curl` or package install by default. Permission bypass flags are never passed.
- **Codex** is started with `exec --json --sandbox workspace-write -C <workspace>`: Codex's own sandbox limits writes to the
  workspace and keeps network access off. Studio never passes the full-access or sandbox-bypass options.
- **Workspace rules:** absolute path, must exist and be a directory, resolved (symlinks followed) before checks, not a filesystem
  root, not the data dir, not inside it, and not a folder that contains it. Checked when queued and again when launched.
  Slot config folders follow the same data-dir rule.
- **Loopback rule unchanged:** the server binds IPv4 loopback only; `0.0.0.0` only inside Docker. Every new route
  (queue, slots, tracker, log, the server-sent-events stream, `DELETE`) goes through the same `Host`, `Origin`,
  `Sec-Fetch-Site` and bearer-token checks as every other `/api/studio/*` call.
- **Arguments and environment:** argv lists only, never a shell; the prompt goes through stdin. The child environment is built
  from an allowlist (`PATH`, home/profile and temp variables, locale, user name) plus the slot's config-folder variable
  (`CLAUDE_CONFIG_DIR` or `CODEX_HOME`) and `NO_COLOR`. API keys, `AETHER_*` variables and cloud credentials are not forwarded.
- **Slots hold no credentials:** `slots.json` has an id, kind, label, a config-folder *path*, model name and flags; unknown
  fields and secret-shaped values are refused, and the API shows the path only. The CLI keeps its own login in that folder.
- **Logs:** CLI output is kept verbatim in `cli-logs/` (owner-only folder and files) and is redacted for secret-shaped text before
  it is served over the log or stream routes or stored as run events. Prompts live in owner-only files and are never logged or returned.
- **Stopping a run:** POSIX: new session, `SIGTERM` then `SIGKILL` of the process group. Windows: new process group and
  `taskkill /T /F /PID`, which ends the whole tree at once (no grace period). All of this is in one module, `aether/studio/proc.py`.
- **Token handling is unchanged:** the same owner token, file, and bearer check; the CLI executor never receives it.

Residual risks: the allowed tools can **read any file your account can read** (not only the workspace), including the CLI's
own login folders, and a model can put what it reads into files it writes; a workspace that contains a slot config folder
(for example your home folder) exposes that login to edits. Prompt injection from files in the workspace can steer the agent
inside these limits. Settings or MCP servers configured in the CLI's own config folder are the CLI's business, not Studio's.
Edits are real: use a dedicated folder, ideally a git checkout you can reset. Redaction is pattern based and will miss
unusual secrets.

## Token handling
Read it with `docker compose exec -T studio cat /data/owner.token` into an environment variable; do not paste it into
shared terminals, tickets or logs. To rotate: `scripts/quickstart.sh down` (deletes the volume and all data), then `up`.

## Reporting
There is no public reporting channel yet; see `docs/FEEDBACK.md` for local, offline feedback. Do not include tokens in feedback.
