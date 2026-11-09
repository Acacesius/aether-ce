# CLI executor (experimental, off by default)

Status: foundation only (job K1). Enabled only when the Studio process runs with `AETHER_EXEC=cli`; with the variable
unset (or any other value) Studio behaves exactly as before: no `cli` executor is registered, no `slots.json` is read,
no process is started. Blueprints cannot select the `cli` executor yet (the blueprint schema still accepts only
`mock` and `adapter`); wiring the schema, the run outcome `limit_stop` and the UI are follow-up jobs.

The CLI executor runs one stage as a subprocess of a locally installed, already signed-in coding CLI (Claude Code or
Codex) in a workspace folder that the owner picked. Studio never sees or stores the credentials: it only knows which
config folder the CLI keeps its own login in.

## Slots (`aether/studio/slots.py`)
A *provider slot* is one signed-in CLI account. Slots live in `<data dir>/slots.json`:

```json
{"version": 1,
 "slots": [{"id": "claude-a", "kind": "claude", "label": "Claude (work)", "config_dir": "/abs/path/to/claude-config",
            "model": null, "enabled": true, "order": 0}],
 "state": {"claude-a": {"blocked_until": "2026-01-01T15:00:00Z", "note": "usage limit"}}}
```

| field | rule |
|---|---|
| `id` | `^[a-z][a-z0-9_-]{0,39}$`, unique |
| `kind` | `claude` (sets `CLAUDE_CONFIG_DIR`) or `codex` (sets `CODEX_HOME`) |
| `label` | 1..80 printable characters |
| `config_dir` | absolute path; must not be, contain, or lie inside the data dir; unique per kind |
| `model` | optional, `^[A-Za-z0-9._:\[\]-]{1,100}$` (passed as `--model` / `-m`) |
| `enabled` | boolean |
| `order` | integer 0..999 (selection order; lower first) |

Unknown fields are refused, so a slot cannot carry a token, key or password; secret-shaped values are refused as well.
`state` is runtime state written by the executor (limit blocks), never by the owner. Writes are atomic (temporary
file in the same folder, `fsync`, `os.replace`) and serialised by an in-process lock; the file is restricted to the
owner through `aether/studio/fsperm.py` (`chmod` on POSIX, `icacls` on Windows). A corrupt file is reported, never
silently replaced.

## Executor (`aether/studio/executors/cli.py`)
`CliExecutor` implements the usual executor protocol (`start`, `poll`, `cancel`, `health`, `capabilities`).
`start` takes the slot from `snapshot.executor.ref` and the workspace from `snapshot.executor.workspace` (or
`snapshot.workspace`); `launch(slot_id, workspace, prompt, ...)` is the same entry point without a run snapshot.

Command lines (argv lists, prompt on **stdin**, never in argv):

* Claude: `claude -p --output-format stream-json --verbose --permission-mode acceptEdits --allowedTools <list>
  --disallowedTools <list> [--model M]`. Allowed: file read/edit/search tools only (`Read Edit MultiEdit Write Glob
  Grep LS NotebookEdit TodoWrite`). Disallowed: `Bash`, web fetch/search, sub-agents, so the CLI has no shell, no
  network tool and cannot `git push`.
* Codex: `codex exec --json --sandbox workspace-write --skip-git-repo-check -C <workspace> [-m M] -` (the
  `workspace-write` sandbox keeps network off).

Workspace rules: must be an existing directory (resolved, symlinks followed), must not be a filesystem root, the data
dir, inside the data dir, or contain the data dir. The process `cwd` is the workspace.

Environment: built from scratch, not inherited. Copied from the Studio environment only when present: `PATH`, `HOME`,
`USERPROFILE`, `HOMEDRIVE`, `HOMEPATH`, `APPDATA`, `LOCALAPPDATA`, `SystemRoot`, `SystemDrive`, `windir`, `COMSPEC`,
`PATHEXT`, `TEMP`, `TMP`, `TMPDIR`, `LANG`, `LC_ALL`, `USER`, `USERNAME`, `LOGNAME`. Then the slot variable
(`CLAUDE_CONFIG_DIR` or `CODEX_HOME`) and `NO_COLOR=1`. Provider API key variables are therefore never passed on.

Timeout: the executor kills the process tree when `timeout_seconds` passes (reason `timeout`); the orchestrator's own
deadline still applies. Cancel kills the tree (grace, then force).

`poll` returns `{"state": "running"}` while running, then one of:
`{"state": "exited", "exit_code": 0}` (success),
`{"state": "exited", "exit_code": N}` (failure; a Claude `result` with `is_error` and exit 0 becomes exit 1),
`{"state": "exited", "exit_code": null, "reason": "timeout" | "cancelled_by_owner" | "executor_error"}`, or
`{"state": "limit_stop", "exit_code": null, "outcome": "limit_stop", "slot": id, "blocked_until": iso}`.
`limit_stop` is not a failure: the slot ran out of quota. The orchestrator does not understand this state yet.

## Events (`aether/studio/cli_events.py`)
Each stdout line is parsed into zero or more events: `assistant_text`, `tool_use`, `tool_result`, `usage` (only when
the CLI reports it), `final_result`, `error`, and `raw` for lines that are not JSON. Unknown JSON types produce
nothing; garbled lines never raise. Both Claude `stream-json` and Codex `exec --json` (current `item.*` /
`turn.*` lines and the older `{"msg": ...}` lines) are understood. The executor forwards them as `stage.log` events
(`{"stream", "text", "redacted", "source": "cli", "event"}`; text passes through the secret redactor) and usage as
`stage.usage` (`tokens` = input + output, `cost_usd_cents` from the CLI's own estimate when present).

Raw log: every stdout line (verbatim) and stderr line (prefixed `[stderr] `) is appended, UTF-8, to
`<data dir>/cli-logs/<run id>.log`, with a `[aether] ...` header per stage attempt. The folder is owner-only.

## Usage-limit detection (`aether/studio/limits.py`)
After a non-successful exit the executor scans the tail of the current attempt's log section. Only text that came from
the CLI itself counts (stderr, non-JSON lines, error events, error results); assistant text and tool output are
ignored, so a model *talking about* limits cannot block a slot. Recognised: Claude "usage limit reached" (including
the legacy `...|<epoch>` form), "hit your limit", "5-hour limit reached ... resets 3pm"; Codex "hit your usage
limit ... try again in 2 hours 5 minutes / at 3:05 PM", `usage_limit_reached`. Reset time parsing: epoch, ISO
timestamp, relative duration, or clock time (optional `(Area/City)` zone, rolled to the next day when already past);
when nothing parses the slot is blocked for 5 hours. The block is persisted in `slots.json` `state` and the
executor refuses to launch a blocked slot (`limit_stop` without starting anything) until the time passes.

## Windows handling (`aether/studio/proc.py`)
`proc.py` is the only module that creates process groups or sends signals. POSIX: `start_new_session=True`,
`os.killpg` (`SIGTERM`, then `SIGKILL` after the grace). Windows: `CREATE_NEW_PROCESS_GROUP`, and
`taskkill /T /F /PID <pid>` to end the tree. CLIs are found with `shutil.which` (which honours `PATHEXT`); for npm
shims a sibling `.exe` is preferred over `.cmd`. Because the prompt goes through stdin and every argv element is a
fixed flag or a validated model name, a `.cmd` shim never receives untrusted text on its command line. No
`shell=True`, no `fcntl`, file I/O is UTF-8 and paths use `pathlib`.

## Safety summary
* Off unless `AETHER_EXEC=cli`; no credentials stored; secret-shaped slot values refused.
* Workspace is owner-chosen, validated, and never the Studio data dir.
* No shell, no network tools, no push tools for Claude; Codex runs in `workspace-write` (no network).
* Minimal environment; provider API keys in the Studio environment are not forwarded.
* The CLI's own edits inside the workspace are real changes: this executor is **not** a demo and not sandboxed by
  Studio beyond the CLI's own permission system. Use a dedicated folder (ideally a git checkout you can reset).

## Queue and driver (job K2; same flag)
With `AETHER_EXEC=cli` the app also creates the queue tables (`queue`, `queue_attempts`, `queue_events`, `slot_state`,
`queue_meta`, `queue_audit`; `aether/studio/exec_queue.py`), and `app.main` starts one driver thread
(`aether/studio/driver.py`). Flag off: no tables, no thread, the routes below answer 404 (DELETE keeps its 405).

* Queue item = a prompt (stored in an owner-only file under `<data dir>/queue-prompts`, referenced by `prompt_ref`), a
  validated workspace and a requested slot (`auto`, `auto:claude`, `auto:codex` or a slot id). States: `queued`,
  `running`, `blocked` (after a restart, see below), `done`, `cancelled`, `failed`.
* Selection: next queued item in position order; first enabled slot in `slots.json` order that is not blocked, is under
  its cap and whose CLI is installed. A pinned slot id is never swapped. Caps: `AETHER_EXEC_MAX_RUNS` (global, default
  1, max 4) and `AETHER_EXEC_SLOT_CAP` (per slot, default 1, max 4). Pause stops new starts only.
* `limit_stop`: attempt recorded as `limit_stop`, item back at the front, next attempt on another slot with a
  `provider_switch {from, to, reason}` event; with no slot free the driver sleeps until the earliest `blocked_until`.
* Source of truth: `blocked_until` lives in `slots.json` `state` (written by the executor / clear-limit);
  `slot_state.blocked_until` is a mirror refreshed every tick. `slot_state.active_count` is authoritative in SQLite.
* Restart: running items become `blocked` (`owner_action_required`, attempt `interrupted`) until the owner resumes
  them, like a non-restartable stage in ORCHESTRATION.md; queued items stay queued; `active_count` resets to 0.
* Orchestrator: a `limit_stop` poll result blocks the stage and run with `owner_action_required` (detail
  `outcome=limit_stop, slot, blocked_until` in the audit entry); Resume re-runs the stage.
* Audit: queue/driver actions go into `queue_audit`, hash-chained with the same hash function as `run_audit` (that
  chain is per Studio run with a foreign key to `runs`, so a queue item has its own chain). Prompt text is never logged.
* Routes (under `/api/studio/`, same Host/Origin/bearer rules): `GET|POST queue`, `GET|DELETE queue/<id>`,
  `POST queue/<id>/resume`, `POST queue/reorder|pause|resume`, `GET queue/audit`, `GET|PUT slots`,
  `POST slots/<id>/clear-limit`. DELETE of a running item kills its process tree through `proc.py`.

## Tracking and live log (AETHER_EXEC=cli)

* `queue_track` (one row per queue item) plus per-attempt columns record provider (slot id and kind), attempts
  (outcome `ok|failed|limit_stop|cancelled`), provider switches, start/end, wall seconds, tokens and cost, final status and
  evidence hashes (sha256 of the raw log and of the final-result text). Tokens and cost are NULL unless the CLI reported them;
  they are never estimated. Every state change also appends to the `queue_audit` chain.
* `GET /api/studio/runs/<id>/log?after=<byte offset>&limit=N&wait=<secs, max 25>`: parsed events and redacted raw text from
  a byte offset, `next_offset`, `done`. Chunks end on a line boundary (never a split line or UTF-8 character); bad or negative
  offsets clamp. `wait` long-polls for new data. `GET .../log/stream` is the same as server-sent events.
* `GET /api/studio/tracker?status=&slot=&provider=&kind=&since=&until=&q=&limit=&offset=`: items plus a summary (counts by
  status, total wall seconds, token/cost sums with the count of runs that reported them).
