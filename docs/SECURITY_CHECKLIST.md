# Security checklist (run before sharing a build)

Commands assume the stack is up (`scripts/quickstart.sh up`). Replace `<id>` with `docker compose ps -q studio`.

| Check | Command | Expected |
|---|---|---|
| Not privileged | `docker inspect -f '{{.HostConfig.Privileged}}' <id>` | `false` |
| Non-root | `docker compose exec -T studio id -u` | `10001` |
| Read-only root fs | `docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' <id>` | `true` |
| Capabilities dropped | `docker inspect -f '{{json .HostConfig.CapDrop}}' <id>` | `["ALL"]` |
| No new privileges | `docker inspect -f '{{json .HostConfig.SecurityOpt}}' <id>` | contains `no-new-privileges:true` |
| Only the data volume mounted | `docker inspect -f '{{range .Mounts}}{{.Destination}} {{end}}' <id>` | `/data` |
| No Docker socket | `docker inspect -f '{{range .Mounts}}{{.Source}} {{end}}' <id>` | no `docker.sock` |
| Loopback only | `docker port <id>` | every line starts with `127.0.0.1:` |
| Not host network | `docker inspect -f '{{.HostConfig.NetworkMode}}' <id>` | not `host` |
| Token required | `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:<port>/api/studio/mode` | `401` |
| Wrong Host refused | `curl -s -o /dev/null -w '%{http_code}' -H 'Host: example.invalid' http://127.0.0.1:<port>/healthz` | `403` |
| No secret-like env | `docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' <id>` | no tokens, keys or provider variables |
| UI is static and same-origin | `curl -sI http://127.0.0.1:<port>/studio/` | `Content-Security-Policy` with `default-src 'none'`, `script-src 'self'`, no inline script |
| Default mode | `docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' <id>` | `AETHER_MODE=custom`, `AETHER_PROVIDER=none`, `AETHER_RUNTIME=docker` |
| Assist off, no cloud | `curl -s -H "Authorization: Bearer $AETHER_TOKEN" http://127.0.0.1:<port>/api/studio/assist/status` | `"available": false`; no `AETHER_STUDIO_ASSIST_*` unless you set it, and then only a loopback `http://` URL |
| No adapters by default | `curl -s -H "Authorization: Bearer $AETHER_TOKEN" http://127.0.0.1:<port>/api/studio/adapters` | `{"adapters": []}` unless you enrolled one; every enrolled adapter shows `granted: []` until you grant scopes |
| Adapter sandbox (if you use adapters) | `python3 -m unittest tests.studio.test_adapter_sandbox` on the release kernel | OK; if Landlock/seccomp is unavailable the adapter refuses to run (exit 70) |
| Audit chain | `curl -s -H "Authorization: Bearer $AETHER_TOKEN" http://127.0.0.1:<port>/api/studio/runs/<run>/audit` | `"ok": true` |
| Release scan | `python3 tools/ce_scan.py .` | exit 0 |
| Clean teardown | `docker compose down -v`, then list containers, volumes, networks named `aether-ce*` | none |
