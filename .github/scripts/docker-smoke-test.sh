#!/usr/bin/env bash
# Smoke test of a built server image, run the way it is deployed: read-only root
# filesystem, no capabilities, no privilege escalation, a fresh volume for /data.
#
# Usage: docker-smoke-test.sh <image>
# Checks: /healthz and /api/v1/server/info answer; logs are JSON lines and never contain
# the secret key; the in-image healthcheck passes; a restart on the same volume reuses
# the key and does not migrate again.
set -euo pipefail

image="${1:?usage: $0 <image>}"
name="tindeerr-smoke-$$"
volume="tindeerr-smoke-data-$$"
port="${SMOKE_PORT:-18787}"
base="http://127.0.0.1:${port}"

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    echo "::group::container logs"
    docker logs "$name" 2>&1 || true
    echo "::endgroup::"
    docker inspect --format '{{json .State}}' "$name" 2>/dev/null || true
  fi
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker volume rm -f "$volume" >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

fail() { echo "::error::$*"; exit 1; }

wait_ready() {
  for _ in $(seq 1 60); do
    if curl -fsS "$base/healthz" >/dev/null 2>&1; then return 0; fi
    if [ "$(docker inspect --format '{{.State.Running}}' "$name")" != "true" ]; then
      fail "container exited during startup"
    fi
    sleep 1
  done
  fail "server not ready after 60 s"
}

docker volume create "$volume" >/dev/null
docker run -d --name "$name" \
  --read-only --tmpfs /tmp:size=64m \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --cpus 2 --memory 512m \
  -v "$volume:/data" -p "127.0.0.1:${port}:8787" \
  "$image" >/dev/null

wait_ready
echo "ready"

health="$(curl -fsS "$base/healthz")"
[ "$health" = '{"status":"ok"}' ] || fail "unexpected /healthz body: $health"

info="$(curl -fsS -D headers.txt "$base/api/v1/server/info")"
python3 - "$info" <<'PY'
import json, sys
info = json.loads(sys.argv[1])
required = {"name", "version", "api_version", "min_app_version", "setup_required",
            "auth_methods", "capabilities", "tmdb_image_base_url"}
missing = required - info.keys()
assert not missing, f"missing fields: {missing}"
assert info["api_version"] == 1, info
assert info["setup_required"] is True, info
assert info["auth_methods"] == [] and info["capabilities"] == [], info
assert info["tmdb_image_base_url"] == "https://image.tmdb.org/t/p/", info
print("server/info OK:", info["name"], info["version"])
PY
grep -qi '^x-content-type-options: nosniff' headers.txt || fail "missing security headers"
grep -qi '^x-request-id: ' headers.txt || fail "missing X-Request-ID"
rm -f headers.txt

problem="$(curl -sS -o /dev/null -w '%{http_code} %{content_type}' "$base/api/v1/nope")"
[ "$problem" = "404 application/problem+json" ] || fail "unexpected 404 response: $problem"

docker exec "$name" tindeerr healthcheck || fail "in-image healthcheck failed"

key="$(docker exec "$name" cat /data/secret.key)"
[ "${#key}" -ge 32 ] || fail "generated key too short"
[ "$(docker exec "$name" stat -c '%a %u' /data/secret.key)" = "600 10001" ] \
  || fail "secret.key must be mode 600, owned by 10001"

check_logs() {
  local logs
  logs="$(docker logs "$name" 2>&1)"
  if grep -qF "$key" <<<"$logs"; then fail "secret key found in logs"; fi
  python3 -c '
import json, sys
lines = [line for line in sys.stdin.read().splitlines() if line.strip()]
assert lines, "no log output"
for line in lines:
    entry = json.loads(line)
    assert {"time", "level", "logger", "message"} <= entry.keys(), line
print(f"{len(lines)} JSON log lines")
' <<<"$logs"
}
check_logs

docker restart "$name" >/dev/null
wait_ready
[ "$(docker exec "$name" cat /data/secret.key)" = "$key" ] || fail "secret key changed on restart"
check_logs
migrations="$(docker logs "$name" 2>&1 | grep -c '"database migrated"' || true)"
[ "$migrations" = "1" ] || fail "expected exactly one migration across restarts, saw $migrations"
docker logs "$name" 2>&1 | grep -q '"source": "file"' || fail "restart did not reuse the key file"
curl -fsS "$base/api/v1/server/info" >/dev/null

echo "smoke test passed"
