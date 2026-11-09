# Windows / POSIX-only audit

Scope: `aether/`, `tools/`, `scripts/` (shell helpers only, not part of the plain-process path). Method: grep + AST gate
(`tests/studio/test_windows_gate.py`) + mocked `os.name`/`sys.platform`. Windows itself was **not** run.

| file:line | issue | severity | fix | status |
|---|---|---|---|---|
| aether/security.py `_posix`, `owned_by_me`, `private_directory` | `st_uid != os.getuid()` and mode `0700` check | high (crash on Windows) | uid check only when `os.getuid` exists; Windows uses `fsperm.restrict` ACLs | FIXED |
| aether/security.py `_open_flags`, `private_open` | `os.O_NOFOLLOW`/`O_NONBLOCK`, mode/nlink checks | high (AttributeError) | `getattr(os, ..., 0)`, `O_BINARY`; mode/nlink checks POSIX-only; `fsperm.restrict` on create | FIXED |
| aether/studio/runner.py:174-183 | `O_NOFOLLOW`, mkdir mode-only security | high | `getattr` flags, `O_BINARY`, `fsperm.restrict` on new artifact dir | FIXED |
| aether/studio/executors/adapter.py:76-79 | `st_mode & 0o022`, `st_uid`, `os.getuid()` in enrollment | high | checks only when POSIX | FIXED |
| aether/studio/executors/adapter.py `sandbox_status`, `Session.__init__`, `_open`, `_static` | adapters could start without sandbox | high (security) | refuse with `sandbox_unavailable` + message; never run unsandboxed | FIXED |
| aether/studio/executors/adapter.py:144 | hardcoded `PATH=/usr/bin:/bin` for adapter env | low | only reachable after sandbox check (Linux) | ACCEPTED-LINUX-ONLY |
| aether/studio/executors/adapter.py cancel | group SIGTERM/forced kill wording | low | goes through `proc.py` | ACCEPTED-LINUX-ONLY |
| aether/studio/adapter_sandbox.py | `import resource`, `ctypes.CDLL(None)`, Landlock/seccomp, prctl | high (import failure) | `resource` optional import; `availability()` call-time check; libc loaded lazily and only when available | FIXED (import) / ACCEPTED-LINUX-ONLY (feature) |
| aether/studio/proc.py:3-119 | `os.killpg`, `SIGKILL`, `start_new_session` | n/a | POSIX branch only; Windows uses `CREATE_NEW_PROCESS_GROUP` + `taskkill /T /F` (argv list) | ACCEPTED (sole permitted location) |
| aether/studio/fsperm.py:34 | `os.chmod` | n/a | POSIX branch; `icacls` argv on Windows | FIXED (already portable) |
| aether/studio/slots.py:159 | atomic write | low | `os.replace`, `O_BINARY`, `fsperm.restrict` | FIXED (already portable) |
| aether/studio/capabilities.py:30 | the string `tmux.*` | none | deny-list entry, not a call | N/A |
| tools/ui_shots.py:86-87,194 | `#!/bin/sh` shim, `chmod`, `/usr/bin:/bin` PATH | low | developer screenshot tool, not used at runtime | ACCEPTED-LINUX-ONLY |
| scripts/*.sh | bash | low | dev/demo helpers; Windows users run `python -m aether` directly | ACCEPTED-LINUX-ONLY |

No `fcntl`, `os.fork`, `shell=True`, `os.rename`, file locking, or tmux calls exist in `aether/` (enforced by the gate).
CLI executor path (exec_queue, driver, executors/cli*, slots, limits, proc, queue_api) has no remaining findings.

Adapters on Windows/macOS: disabled with the message "adapter sandbox unavailable: it needs Linux (Landlock + seccomp)...";
CLI executors are unaffected.
