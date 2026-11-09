# Quick start

Requirements: Linux (or macOS) with Docker and the Compose plugin, `curl`, `sed`, `grep`, and a free local TCP port.
**The only network access needed is pulling the base image `python:3.13-slim` during the build.** Nothing is installed
with pip/apt/npm and the running container makes no outbound connections.

## 1. Start
```sh
AETHER_PUBLIC_PORT=8080 scripts/quickstart.sh up
```
This runs `docker compose up -d --build`, waits for the container health check, and prints next steps. It never prints the
owner token. The port is published on `127.0.0.1` only. (Plain alternative: `docker compose up -d --build`.)

## 1b. Use the web UI
Open `http://127.0.0.1:<port>/` (it redirects to `/studio/`). Read the owner token into your clipboard or shell without
printing it to shared screens: `docker compose exec -T studio cat /data/owner.token`, then paste it into the page. The
token stays in that browser tab only (`sessionStorage`) and is sent only to this server.

| Tab | Try this |
|---|---|
| **Quick Run** | choose "Produces an evidence artifact" and run it; every line is labelled `DEMO` |
| **Builder** | open a blueprint, add a stage, set `needs` to build a DAG, give a stage a role (planner / worker / reviewer), validate, save |
| **Library** | find your blueprint, see its versions, export it, import it back (unchanged bundles import idempotently) |
| **Runs** | open a run: stage graph, events, evidence, *Audit trail* (hash chain verified or not); cancel a 30 s run; a run waiting for approval shows the approval and resumes when you grant it |

Import the multi-stage example (`examples/demo-harness-dag.json`: planner, two parallel workers, reviewer, all DEMO) with
the `curl` steps below or paste its `spec` into the Builder, then run it from the Library.

## 2. Get the owner token (into your shell, not onto the screen)
```sh
export AETHER_TOKEN="$(docker compose exec -T studio cat /data/owner.token)"
export P=8080 U=http://127.0.0.1:8080/api/studio
H=(-H "Authorization: Bearer $AETHER_TOKEN" -H "Origin: http://127.0.0.1:$P" -H "Content-Type: application/json")
curl -s "${H[@]}" $U/mode          # {"demo_only": true, "mode": "custom", "provider": "none", ...}
```
Requests must use `Host: 127.0.0.1:<port>` or `localhost:<port>`; `POST`/`PUT` also need a matching `Origin` header.
Without the token the API answers `401`.

## 3. Create a definition, run it, watch it
```sh
D=$(curl -s "${H[@]}" -X POST --data @examples/demo-harness.json $U/definitions | sed -n 's/.*"id": "\(hd_[A-Z0-9]*\)".*/\1/p')
R=$(curl -s "${H[@]}" -X POST -H "Idempotency-Key: first-run" -d "{\"definition_id\":\"$D\"}" $U/runs | sed -n 's/.*"id": "\(run_[A-Z0-9]*\)".*/\1/p' | head -1)
curl -s "${H[@]}" $U/runs/$R                         # status: queued -> running -> succeeded, label DEMO
curl -s "${H[@]}" "$U/runs/$R/events?cursor=0"       # events; pass next_cursor to continue; poll_after_ms says when
```
`POST /runs` requires an `Idempotency-Key` header: repeating the same key returns the same run. To override a declared
input: add `"overrides": {"topic": "beta"}` to the body.

## 4. Inspect artifacts, logs and evidence
```sh
curl -s "${H[@]}" $U/runs/$R/evidence                # artifact names + SHA-256, events hash, snapshot hash, sealed flag
docker compose exec -T studio ls /data/artifacts/$R
docker compose exec -T studio cat /data/artifacts/$R/<sha256-from-evidence>
```
Logs are `stage.log` events in the event stream. The API serves artifact metadata and hashes; content is read from the volume.

## 4b. A multi-stage DAG run (planner, workers, reviewer)
```sh
DD=$(curl -s "${H[@]}" -X POST --data @examples/demo-harness-dag.json $U/definitions | sed -n 's/.*"id": "\(hd_[A-Z0-9]*\)".*/\1/p')
RD=$(curl -s "${H[@]}" -X POST -H "Idempotency-Key: dag-1" -d "{\"definition_id\":\"$DD\"}" $U/runs | sed -n 's/.*"id": "\(run_[A-Z0-9]*\)".*/\1/p' | head -1)
curl -s "${H[@]}" $U/runs/$RD                         # the stage graph: plan -> work-a, work-b -> review; status succeeded
curl -s "${H[@]}" $U/runs/$RD/audit                    # hash-chained audit trail and its verification result
```
DEMO limits: the mock executor scripts every step, including the reviewer's verdict (`mock:verdict:pass`) and it cannot
propose follow-up stages. A real planner or reviewer needs an adapter. See `docs/studio/ORCHESTRATION.md`.

## 5. A failed run and a cancelled run
```sh
DF=$(curl -s "${H[@]}" -X POST --data @examples/demo-harness-failing.json $U/definitions | sed -n 's/.*"id": "\(hd_[A-Z0-9]*\)".*/\1/p')
curl -s "${H[@]}" -X POST -H "Idempotency-Key: fail-1" -d "{\"definition_id\":\"$DF\"}" $U/runs      # ends failed, reason exit_nonzero, exit_code 3

DC=$(curl -s "${H[@]}" -X POST --data @examples/demo-harness-cancellable.json $U/definitions | sed -n 's/.*"id": "\(hd_[A-Z0-9]*\)".*/\1/p')
RC=$(curl -s "${H[@]}" -X POST -H "Idempotency-Key: cancel-1" -d "{\"definition_id\":\"$DC\"}" $U/runs | sed -n 's/.*"id": "\(run_[A-Z0-9]*\)".*/\1/p' | head -1)
curl -s "${H[@]}" -X POST -d '{}' $U/runs/$RC/cancel                                                # 202; ends cancelled, reason cancelled_by_owner
```

## 6. Export and import
```sh
curl -s "${H[@]}" -X POST -d "{\"definition_ids\":[\"$D\"]}" $U/export > bundle.json
curl -s "${H[@]}" -X POST --data @bundle.json $U/import        # unchanged bundle: "created": [] (idempotent)
```
The bundle contains no ids or timestamps in its hash and no secret-named fields. Import rejects secret-shaped values,
unknown fields, unapproved capabilities and hash mismatches, all-or-nothing.

## 7. Optional: AI assist (off by default) and adapters (opt-in)
- **AI assist** is off: `curl -s "${H[@]}" $U/assist/status` answers `{"available": false, "detail": "unavailable: no provider
  configured"}` and the Builder says so; everything else works unchanged. To enable it you run a model **on a loopback
  address inside the container's own network namespace** and set `AETHER_STUDIO_ASSIST_URL` and
  `AETHER_STUDIO_ASSIST_MODEL` in the `environment:` of `compose.yaml`. A model on the host is not loopback from inside the
  container and is refused by design; there is no cloud option. Details and threat model: `docs/studio/AI_ASSIST.md`.
- **Adapters** connect a local harness through `aether.studio.adapter/v1`. Nothing is enrolled by default and an enrolled
  adapter has **no** permission until you grant scopes. A runnable example is `examples/adapters/echo_adapter.py` (it only
  echoes). The container image does not include it and the container cannot see host files, so enrolling an adapter in
  Docker means adding your own read-only script mount (not group/world-writable, outside `/data`) to `compose.yaml`.
  Protocol, sandbox and enrolment API: `docs/studio/ADAPTER_PROTOCOL.md`.

## One command for steps 3-6
`scripts/demo_flow.sh` performs the walkthrough (success, DAG, failure, cancel, export/import, UI and assist checks) and
prints `RESULT PASS` when every step behaved as documented.

## Stop and clean up
```sh
scripts/quickstart.sh down        # docker compose down -v, then removes the aether-ce:local image
```
Resource names used: project `aether-ce`, volume `aether-ce-data`, network `aether-ce-net`, image `aether-ce:local`.
Set `AETHER_PUBLIC_PORT` (default `8080`) to avoid port clashes, and `AETHER_STUDIO_MAX_WORKERS` (1-4) for concurrency.
