# AETHER Community Edition (CE)

A small, local harness studio. You define a *harness* (a versioned blueprint of stages), run it, watch its events,
inspect its evidence, cancel or resume it, and export/import definitions. In this release the only built-in executor is a
**mock** that simulates work and takes no real action: every run, event and artifact it produces is labelled `DEMO`.
The demo flows use the mock executor. In the plain-process start (below) the **CLI executor** is also enabled: it queues
prompts for your own signed-in Claude Code / Codex command line tools ([docs/studio/CLI_EXECUTOR.md](docs/studio/CLI_EXECUTOR.md)).
Adapters are a separate, opt-in, sandboxed way to connect a local harness (off by default).

CE has a web UI and an HTTP API. It runs as a plain Python process (or optionally in one Docker container), always
listening on `127.0.0.1` only.

**Status: pre-release / alpha.** Licence: MIT (see `LICENSE`). No external tester has run it yet; Windows and
macOS are untested. See [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).

## What you get
| Area | What it does |
|---|---|
| **Workspace / Slots / Tracker** | (plain-process mode) queue prompts for your Claude Code / Codex CLIs, manage signed-in accounts as slots with automatic switching on usage limits, and review history |
| **Quick Run** | pick a canned DEMO scenario (quick success, artifact, failure, 30 s run to cancel) and run it in one click |
| **Builder** | edit a blueprint: stages, `needs` (a DAG), roles (planner / worker / reviewer), capabilities, limits, retries |
| **Library** | versioned blueprints (new version only when the spec changes), archive, export / import |
| **Runs** | live status, per-stage graph, events, evidence with SHA-256, hash-chained audit trail, cancel, approve, resume |
| **Orchestration** | stage DAG, bounded fan-out, retries, wall-clock budgets, restart recovery: [docs/studio/ORCHESTRATION.md](docs/studio/ORCHESTRATION.md) |
| **Adapters (opt-in)** | connect a local harness through a stdio protocol inside a Landlock + seccomp sandbox: [docs/studio/ADAPTER_PROTOCOL.md](docs/studio/ADAPTER_PROTOCOL.md) |
| **AI assist (optional, off)** | draft a blueprint from a description with a model on a loopback address; never cloud: [docs/studio/AI_ASSIST.md](docs/studio/AI_ASSIST.md) |

## What it is not
No cloud model providers of its own, no remote access (loopback only). Real work comes only from two opt-in paths: the CLI
executor, which drives CLIs you installed and signed in to yourself, and adapters, each of which starts with **no** permission. See [docs/SECURITY.md](docs/SECURITY.md).

## Quick start (plain Python process, primary)
Needs Python 3.11 or newer and nothing else (standard library only; no venv, no `pip`, no Docker).

**Windows 10/11:** get the repository (Download ZIP and Extract All, or `git clone`) and double-click **`start.bat`**
(`start.ps1` is the PowerShell alternative). Step-by-step guide:
[docs/WINDOWS.md](docs/WINDOWS.md). **Windows is untested by the author.**

**Linux / macOS:**
```sh
git clone <this repository> aether-ce && cd aether-ce
./start.sh                      # or: python3 -m aether.studio serve --open
```
Studio listens on **http://127.0.0.1:8080/** (`--port N` to change it, `--data-dir PATH` to move its data). The start
output names the owner token file (`owner.token` in the data folder, never printed); open it and paste the token into
the page. Then add your signed-in Claude/Codex accounts in the **Slots** tab and a first run in **Workspace**.
`python3 -m aether.studio doctor` prints an environment report you can send back when something fails.

## Quick start (Docker, secondary; Linux with the compose plugin)
This is the demo-only container path (mock executor, no CLI executor).
```sh
git clone <this repository> aether-ce && cd aether-ce
AETHER_PUBLIC_PORT=8080 scripts/quickstart.sh up     # build, start, wait until healthy
scripts/demo_flow.sh                                 # success / failed / cancelled / DAG DEMO runs, export, import
scripts/quickstart.sh down                           # stop and delete container, network and data volume
```
Open **http://127.0.0.1:8080/** (use your `AETHER_PUBLIC_PORT`) and paste the owner token when asked; the token is read
with `docker compose exec -T studio cat /data/owner.token` and is never printed by the scripts.
The only network access needed is pulling the base image (`python:3.13-slim`) while building. At runtime CE makes no
outbound connections. Full walkthrough (UI and `curl`): [docs/QUICKSTART.md](docs/QUICKSTART.md).

## Layout
| Path | Purpose |
|---|---|
| `aether/studio/` | the application (stdlib Python only) and its UI (`aether/studio/ui/`) |
| `aether/security.py` | owner-token storage with strict file modes |
| `examples/` | DEMO harness definitions (success, failing, cancel-able, multi-stage DAG) and `examples/adapters/` (a local adapter) |
| `tests/` | unit tests: `python3 -m unittest discover -s tests` |
| `tools/ce_scan.py` | release scanner (private literals, secrets, host paths, risky config) |
| `start.bat`, `start.ps1`, `start.sh` | plain-process launchers (Windows / Linux / macOS) |
| `scripts/` | Docker helpers: `quickstart.sh`, `demo_flow.sh` |
| `docs/`, `feedback/` | documentation and a local feedback template |

## Tests
```sh
python3 -m unittest discover -s tests      # needs only the Python 3 standard library (developed on 3.13, the image version)
python3 tools/ce_scan.py .                 # exit 0 = clean (run in the canonical repo; a clone has a remote and is reported)
```
The adapter sandbox tests need a Linux kernel with Landlock and seccomp and are not skipped elsewhere: on other systems
they fail loudly, because an adapter never runs unconfined. The browser-UI syntax test is skipped when `node` is absent.

## Feedback
Local, structured, no telemetry: [docs/FEEDBACK.md](docs/FEEDBACK.md).
