# Release checklist (owner)

Nothing here has been done on the owner's behalf. Tick items only with evidence.

- [x] Licence: MIT, `LICENSE` holds the standard text with "Copyright (c) 2026 Brandon" (`tools/export_check.py` verifies it).
- [ ] Run the scanner in the canonical repository, not in a `git clone` of it: a clone carries a remote and a path to its source, which the scanner (correctly) reports as `git_remote`/`home_path` findings.
- [ ] Owner private-literal denylist created outside the repository and `AETHER_CE_DENYLIST=<file> python3 tools/ce_scan.py .` exits 0 on the tree and the history.
- [ ] Owner reviewed `ce_scan_allow.json` (empty in this export) and the full file list (`git ls-files`).
- [ ] `python3 -m unittest discover -s tests` passes in a fresh clone.
- [ ] `scripts/quickstart.sh up` + `scripts/demo_flow.sh` pass in a fresh clone (UI root and `/api` reachable on `127.0.0.1` only, DAG demo run succeeds, assist reports unavailable); `down` leaves no `aether-ce` container, volume or network.
- [ ] Defaults re-checked: `AETHER_MODE=custom`, `AETHER_PROVIDER=none`, `AETHER_RUNTIME=docker`; no adapter enrolled; no `AETHER_STUDIO_ASSIST_*` variable in `compose.yaml` / `.env.example`.
- [ ] Adapters: sandbox tests pass on the release kernel (`tests/studio/test_adapter_sandbox.py`); the only shipped adapter is the echo example; nothing in `examples/adapters/` performs real work.
- [ ] AI assist: URL must be loopback `http://` only (no cloud provider, no key variable anywhere); `docs/studio/AI_ASSIST.md` matches the code.
- [ ] Orchestration: `docs/studio/ORCHESTRATION.md` limits match `schema.py` / `runner.py`; example DAG harness is labelled DEMO.
- [ ] Live isolation: the private repository's live-isolation tests pass, and the CE tree contains no live service, URL, host name or token reference.
- [ ] `docs/SECURITY_CHECKLIST.md` completed.
- [ ] Base image decision: keep the tag or pin by digest.
- [ ] At least one external tester ran the quick start and returned `feedback/TESTER_FEEDBACK.md`.
- [ ] Windows (and macOS) result recorded, or both explicitly marked unsupported.
- [ ] Relative links in the Markdown docs resolve (`tests/test_docs.py`).
- [ ] Repository history is the single export commit (`git rev-list --count HEAD` is 1) and has no remote until the owner adds one.
- [ ] Publication target and visibility chosen by the owner (out of scope for this export).

## Export and friend zip hand-off
- [ ] `python3 tools/export_check.py .` exits 0 (no private literals, MIT `LICENSE`, required files incl. `start.bat`, `docs/WINDOWS.md`). Allowlisted fixtures live in `export_check_allow.json` (exact rule/file/line-hash entries, each with a reason); review it.
- [ ] `python3 tools/export_check.py --plan` read through; steps followed by the owner (it prints only, it never pushes or deletes).
- [ ] Fresh export: tracked files copied into an empty directory, `git init`, one commit "AETHER Community Edition 0.1"; `export_check.py` and `ce_scan.py` both exit 0 on that directory.
- [ ] Friend zip made from that commit (`git archive --format=zip`), with `start.bat` at the zip root (`unzip -l <zip>`); hand over only the zip.
- [ ] Friend follows `docs/WINDOWS.md`; their result is recorded in `feedback/TESTER_FEEDBACK.md`.
