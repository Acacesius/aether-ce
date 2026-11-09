#!/usr/bin/env bash
# Walk the DEMO flow against a running CE (scripts/quickstart.sh up). Needs: curl, docker compose, sed, grep.
# Reads the owner token into a variable only; it is never printed. Everything here is DEMO (simulated, no real action).
# Exit 0 = every step behaved as documented.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
PORT="${AETHER_PUBLIC_PORT:-8080}"
U="http://127.0.0.1:$PORT/api/studio"
TOKEN="$(docker compose exec -T studio cat /data/owner.token)" || { echo "cannot read token; is CE running?" >&2; exit 2; }
H=(-H "Authorization: Bearer $TOKEN" -H "Origin: http://127.0.0.1:$PORT" -H "Content-Type: application/json")
FAILS=0
api() { curl -sS "${H[@]}" "$@"; }
idof() { sed -n "s/.*\"id\": \"\\($1_[A-Z0-9]*\\)\".*/\\1/p" | head -1; }
status() { api "$U/runs/$1" | grep -o '"status": "[a-z]*"' | tail -1 | sed 's/.*: "\(.*\)"/\1/'; }
wait_for() { # wait_for <run> <status> [seconds]
  for _ in $(seq 1 "${3:-30}"); do [ "$(status "$1")" = "$2" ] && return 0; sleep 1; done; return 1
}
has() { api "$1" | tr -d ' \n' | grep -q "$2"; }   # has <url> <pattern without spaces>: the token stays in this shell
check() { if "${@:2}"; then echo "PASS  $1"; else echo "FAIL  $1"; FAILS=$((FAILS+1)); fi; }
new_def() { api -X POST --data @"examples/$1" "$U/definitions" | idof hd; }
new_run() { api -X POST -H "Idempotency-Key: $2-$RANDOM$RANDOM" -d "{\"definition_id\":\"$1\"}" "$U/runs" | idof run; }

echo "== 1. mode (expects demo_only, provider none)"; api "$U/mode" | grep -o '"demo_only": true'
echo "== 1b. UI and assist (UI root redirects to /studio/; assist is off without a provider)"
UIH=(-H "Host: 127.0.0.1:$PORT")
check "UI root redirects to /studio/" test "$(curl -sS -o /dev/null -w '%{http_code} %{redirect_url}' "${UIH[@]}" "http://127.0.0.1:$PORT/")" = "302 http://127.0.0.1:$PORT/studio/"
check "UI page served" test "$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/studio/")" = 200
check "API refuses a request without a token" test "$(curl -sS -o /dev/null -w '%{http_code}' "$U/mode")" = 401
check "assist reports unavailable, no provider" has "$U/assist/status" '"available":false'
echo "== 2. create definition from examples/demo-harness.json"
D="$(new_def demo-harness.json)"; echo "definition $D"; check "definition created" test -n "$D"
echo "== 3. run DEMO"
R="$(new_run "$D" ok)"; echo "run $R"; check "run succeeded" wait_for "$R" succeeded
echo "== 4. events / logs (type + text)"
api "$U/runs/$R/events?limit=50" | grep -o '"text": "[^"]*"\|"type": "[a-z.]*"' | sed 's/^/  /'
echo "== 5. artifacts and evidence"
api "$U/runs/$R/evidence"; echo
SHA="$(api "$U/runs/$R/evidence" | grep -o '"sha256": "[0-9a-f]\{64\}"' | head -1 | sed 's/.*: "\(.*\)"/\1/')"
echo "artifact content (read from the data volume; the API serves metadata and hashes only):"
docker compose exec -T studio cat "/data/artifacts/$R/$SHA"
echo "== 5b. multi-stage DAG DEMO run (planner -> workers -> reviewer), audit trail"
DD="$(new_def demo-harness-dag.json)"; RD="$(new_run "$DD" dag)"; check "DAG run succeeded" wait_for "$RD" succeeded
api "$U/runs/$RD" | grep -o '"key": "[a-z-]*"' | sort -u | sed 's/^/  stage /'
check "reviewer verdict recorded" has "$U/runs/$RD/events?limit=200" 'review.verdict'
check "audit chain verifies" has "$U/runs/$RD/audit" '"ok":true'
echo "== 6. failing DEMO run"
DF="$(new_def demo-harness-failing.json)"; RF="$(new_run "$DF" fail)"; check "failing run ends failed" wait_for "$RF" failed
api "$U/runs/$RF" | grep -o '"reason": "[a-z_]*"\|"exit_code": [0-9]*' | head -2 | sed 's/^/  /'
echo "== 7. cancel a running DEMO run"
DC="$(new_def demo-harness-cancellable.json)"; RC="$(new_run "$DC" cancel)"; check "run is running" wait_for "$RC" running
api -X POST -d '{}' "$U/runs/$RC/cancel" | grep -o '"status": "[a-z]*"' | head -1
check "run ends cancelled" wait_for "$RC" cancelled
api "$U/runs/$RC" | grep -o '"reason": "[a-z_]*"' | head -1 | sed 's/^/  /'
echo "== 8. export and import (import of an unchanged bundle is idempotent: nothing new is created)"
api -X POST -d "{\"definition_ids\":[\"$D\"]}" "$U/export" > "${TMPDIR:-/tmp}/aether-ce-bundle.json"
echo "bundle bytes: $(wc -c < "${TMPDIR:-/tmp}/aether-ce-bundle.json")"
api -X POST --data @"${TMPDIR:-/tmp}/aether-ce-bundle.json" "$U/import" | grep -o '"created": \[[^]]\{0,3\}\]'
rm -f "${TMPDIR:-/tmp}/aether-ce-bundle.json"
unset TOKEN
echo "SUMMARY failures=$FAILS"; [ "$FAILS" = 0 ] && echo "RESULT PASS" || echo "RESULT FAIL"
exit $((FAILS > 0))
