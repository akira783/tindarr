#!/usr/bin/env bash
# Smoke test of a built server image, run the way it is deployed: read-only root
# filesystem, no capabilities, no privilege escalation, a fresh volume for /data.
#
# Usage: docker-smoke-test.sh <image>
# Checks: /healthz and /api/v1/server/info answer; the built web console is in the
# image and, once the server serves it (lot 2c), / answers with index.html under the
# console CSP; logs are JSON lines and never contain the secret key; the in-image
# healthcheck passes; a restart on the same volume reuses the key and does not migrate
# again.
set -euo pipefail

image="${1:?usage: $0 <image>}"
name="tindarr-smoke-$$"
volume="tindarr-smoke-data-$$"
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

# The Node build stage put the console in the image (docs/architecture.md: the runtime
# stage copies web/dist to TINDARR_WEB_DIR).
docker exec "$name" test -f /app/web/index.html || fail "the console is missing from /app/web"
docker exec "$name" sh -c 'ls /app/web/assets/*.js >/dev/null 2>&1' \
  || fail "the console has no hashed asset in /app/web/assets"
if docker exec "$name" sh -c 'command -v node >/dev/null 2>&1'; then
  fail "the runtime image must not contain Node"
fi

# Serving it is lot 2c. While the server answers something else, say so and move on, so
# this check turns itself on the day the server part lands.
console_status="$(curl -sS -o console.html -D console-headers.txt -w '%{http_code}' "$base/")"
if [ "$console_status" = "200" ]; then
  grep -q '<div id="root"></div>' console.html || fail "/ did not serve the console index.html"
  csp="$(grep -i '^content-security-policy:' console-headers.txt || true)"
  [ -n "$csp" ] || fail "/ has no Content-Security-Policy"
  for directive in "default-src 'self'" "script-src 'self'" "style-src 'self'" \
      "object-src 'none'" "base-uri 'none'" "frame-ancestors 'none'" \
      "require-trusted-types-for 'script'"; do
    grep -qF "$directive" <<<"$csp" || fail "console CSP is missing: $directive"
  done
  grep -qi '^cache-control: no-store' console-headers.txt || fail "index.html must not be cached"
  grep -qi '^cross-origin-opener-policy: same-origin' console-headers.txt \
    || fail "index.html needs Cross-Origin-Opener-Policy: same-origin"
  grep -qi '^x-frame-options: deny' console-headers.txt || fail "index.html needs X-Frame-Options: DENY"

  asset="$(grep -o '/assets/[^"]*\.js' console.html | head -1)"
  [ -n "$asset" ] || fail "index.html references no hashed asset"
  curl -fsS -o /dev/null -D asset-headers.txt "$base$asset" || fail "$asset is not served"
  grep -qi '^cache-control: public, max-age=31536000, immutable' asset-headers.txt \
    || fail "hashed assets must be cached as immutable"
  missing="$(curl -sS -o /dev/null -w '%{http_code}' "$base/assets/does-not-exist.js")"
  [ "$missing" = "404" ] || fail "a missing asset must be a 404, not index.html (got $missing)"
  echo "console served under the console CSP"
else
  echo "::notice::/ answered $console_status: the server does not serve the console yet (lot 2c)."
fi
rm -f console.html console-headers.txt asset-headers.txt

docker exec "$name" tindarr healthcheck || fail "in-image healthcheck failed"

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
