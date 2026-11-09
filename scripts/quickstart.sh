#!/usr/bin/env bash
# AETHER CE quick start. Usage: scripts/quickstart.sh [up|token-help|status|down]
#   up          build the image, start the container, wait until healthy, print next steps (default)
#   token-help  print the command that reads the owner token (the token itself is never printed here)
#   status      show container state and health
#   down        stop and REMOVE the container, network, data volume and the local image (deletes all CE data)
# Only network use: pulling the base image during `docker compose build`. Runtime needs no network.
# Port: AETHER_PUBLIC_PORT (default 8080), published on 127.0.0.1 only.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
PORT="${AETHER_PUBLIC_PORT:-8080}"
export AETHER_PUBLIC_PORT="$PORT"
cmd="${1:-up}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing required tool: $1" >&2; exit 2; }; }
need docker
docker compose version >/dev/null 2>&1 || { echo "docker compose plugin required" >&2; exit 2; }

health() { docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q studio)" 2>/dev/null; }

case "$cmd" in
  up)
    if docker ps -q --filter "publish=$PORT" | grep -q .; then
      echo "port $PORT is already published by another container; set AETHER_PUBLIC_PORT to a free port" >&2; exit 2
    fi
    docker compose up -d --build || { echo "compose up failed" >&2; exit 1; }
    st=""
    for _ in $(seq 1 90); do
      st="$(health)"; [ "$st" = healthy ] && break; sleep 1
    done
    if [ "$st" != healthy ]; then
      echo "container did not become healthy (state: ${st:-unknown}); logs:" >&2
      docker compose logs --no-color --tail 30 studio >&2; exit 1
    fi
    echo "AETHER CE is healthy on http://127.0.0.1:$PORT (web UI at /, API at /api/studio). All runs are DEMO."
    "$0" token-help
    ;;
  token-help)
    cat <<MSG
Read the owner token into your shell (it is not printed by this script):
  export AETHER_TOKEN="\$(docker compose exec -T studio cat /data/owner.token)"
Check access (expects HTTP 200):
  curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer \$AETHER_TOKEN" http://127.0.0.1:$PORT/api/studio/mode
Open the UI:                 http://127.0.0.1:$PORT/   (paste the token when asked)
Walk through the DEMO flow:  scripts/demo_flow.sh   (or follow docs/QUICKSTART.md)
Tear down (deletes data):    scripts/quickstart.sh down
MSG
    ;;
  status) docker compose ps; echo "health: $(health)";;
  down) docker compose down -v; docker image rm aether-ce:local >/dev/null 2>&1 || true;;
  *) sed -n 2,8p "$0"; exit 2;;
esac
