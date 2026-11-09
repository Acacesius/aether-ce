# CE tester feedback (local file, no upload)

- Tester (alias is fine):
- Date:
- CE commit (`git rev-parse --short HEAD`):
- OS and version:
- Docker / Compose versions (`docker --version`, `docker compose version`):
- Tested on: [ ] Linux  [ ] macOS  [ ] Windows (WSL 2)  [ ] Windows (PowerShell)

## Steps (mark PASS / FAIL / SKIPPED, add exit codes and a one-line note)
| Step | Result | Exit code | Note |
|---|---|---|---|
| `scripts/quickstart.sh up` reached healthy | | | |
| Token read via `docker compose exec` (not printed) | | | |
| Create definition from `examples/demo-harness.json` | | | |
| DEMO run succeeded, events visible | | | |
| Artifact metadata and content found | | | |
| Failing example ended `failed` | | | |
| Cancel-able example ended `cancelled` | | | |
| Export and import | | | |
| Web UI at `http://127.0.0.1:<port>/` opened and accepted the token | | | |
| UI: Quick Run, Builder, Library, Runs each opened; a DEMO run was started from the UI | | | |
| Multi-stage example (`examples/demo-harness-dag.json`) succeeded; audit trail verified | | | |
| AI assist showed "not available" and the Builder still worked (unless you enabled it) | | | |
| `scripts/demo_flow.sh` printed `RESULT PASS` | | | |
| `python3 -m unittest discover -s tests` | | | |
| `scripts/quickstart.sh down` left nothing behind | | | |

## Problems (one per block)
- What I did:
- What I expected:
- What happened (error text, with any token removed):
- Severity: [ ] blocked  [ ] confusing  [ ] cosmetic

## Clarity
- Which doc was hardest to follow?
- What did you expect CE to do that it does not?

## Security observations
- Anything that looked exposed beyond 127.0.0.1, or any secret visible in output?

## Confirmations
- [ ] I did not include an owner token in this file.
