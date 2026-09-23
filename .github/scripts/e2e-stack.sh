#!/usr/bin/env bash
# The stack the end-to-end scenario runs against (docs/roadmap.md, step 2, check b).
#
#   e2e-stack.sh up [target …]   start the media servers, seed them, start one Tindarr
#                                server per target, write the environment file
#   e2e-stack.sh check-logs      fail if a Tindarr server logged an error
#   e2e-stack.sh logs [dir]      copy every container's log into <dir> (CI artifacts)
#   e2e-stack.sh down            remove everything this script created
#
# One **target** is one media server product plus its own Tindarr server on its own
# port and its own fresh /data volume. They are independent: a Tindarr server is set up
# once, against one media server, and pointing an existing one somewhere else is a
# different scenario (docs/auth.md, section 6) that belongs to the security suite.
#
# Every Tindarr server listens on 127.0.0.1 in the **host** network namespace, and so do
# the media servers' published ports. That is what makes the scenario work without a
# reverse proxy:
#   - the browser reaches the console at http://127.0.0.1:<port>, a loopback address
#     with a loopback Host, which browsers treat as a secure context and Tindarr
#     accepts for its `__Host-` cookies over plain HTTP (docs/auth.md, section 2);
#   - `public_url` can be that same address, and the server's own verification request
#     to `<public_url>/api/v1/server/info` reaches itself (docs/auth.md, section 10);
#   - the media server URL is the same string for the seeding script and for Tindarr.
#
# The Tindarr containers run exactly as they are deployed: read-only root filesystem, no
# capabilities, no privilege escalation, state in a volume.
#
# Set E2E_IMAGE=source to run the server from the working tree with uv instead of an
# image (no Docker build needed locally; `npm run build` in web/ first). CI uses the
# image it has just built.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Media server images, pinned by digest. Renovate keeps them up to date through the
# customManager in renovate.json.
# renovate: datasource=docker depName=jellyfin/jellyfin versioning=loose
JELLYFIN_IMAGE="jellyfin/jellyfin:12.1.20260915-010956@sha256:78d3ea1207d1322471fcac39a614f004f2ccf7e878f95ab2977d752f07e4dd7e"
# The oldest Jellyfin Tindarr supports (docs/auth.md, section 4). Pinned on purpose:
# it must keep working when the current release moves on, so it is not renovated.
JELLYFIN_MIN_IMAGE="jellyfin/jellyfin:10.10.7@sha256:7ae36aab93ef9b6aaff02b37f8bb23df84bb2d7a3f6054ec8fc466072a648ce2"
# renovate: datasource=docker depName=emby/embyserver versioning=loose
EMBY_IMAGE="emby/embyserver:4.10.0.40@sha256:3aafff933d3f28d23ed0bc201022abe71c0aa80deb17177566c726b9bbc686c6"

# target | media server kind | image | media server port | console port
TARGETS=(
  "jellyfin|jellyfin|${JELLYFIN_IMAGE}|18096|18801"
  "jellyfin_min|jellyfin|${JELLYFIN_MIN_IMAGE}|18098|18803"
  "emby|emby|${EMBY_IMAGE}|18097|18802"
)

IMAGE="${E2E_IMAGE:-tindarr-server:e2e}"
ENV_FILE="${E2E_ENV_FILE:-${repo_root}/e2e.env}"
STATE_DIR="${E2E_STATE_DIR:-${repo_root}/.e2e-state}"
READY_TIMEOUT="${E2E_READY_TIMEOUT:-300}"
ADMIN_USER="tindarr-admin"
PLAIN_USER="tindarr-user"
PREFIX="tindarr-e2e"

fail() { echo "::error::e2e-stack: $*" >&2; exit 1; }
note() { echo "e2e-stack: $*"; }

field() { echo "$1" | cut -d'|' -f"$2"; }

selected_targets() {
  if [ "$#" -eq 0 ]; then
    printf '%s\n' "${TARGETS[@]}"
    return
  fi
  local wanted found=""
  for wanted in "$@"; do
    local row
    for row in "${TARGETS[@]}"; do
      if [ "$(field "$row" 1)" = "$wanted" ]; then echo "$row"; found="yes"; fi
    done
    [ -n "$found" ] || fail "unknown target '$wanted'"
  done
}

wait_for() { # wait_for <url> <what>
  local url="$1" what="$2" deadline=$((SECONDS + READY_TIMEOUT))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if curl -fsS --noproxy '*' -o /dev/null "$url"; then return 0; fi
    sleep 1
  done
  fail "$what did not answer at $url after ${READY_TIMEOUT} s"
}

# --------------------------------------------------------------------------- up

start_media_server() {
  local name="$1" kind="$2" image="$3" port="$4"
  if [ "$(docker inspect --format '{{.State.Running}}' "$name" 2>/dev/null)" = "true" ]; then
    note "$name is already running"
    return
  fi
  local extra=()
  # Emby's entrypoint drops to this uid/gid; without them it runs the server as root
  # and writes a /config only root can read afterwards.
  [ "$kind" = "emby" ] && extra=(-e UID=1000 -e GID=1000 -e GIDLIST=1000)
  # No volume: the anonymous ones die with the container, so every run is a virgin
  # server whose first-run wizard is still pending.
  docker run -d --name "$name" "${extra[@]}" \
    -p "127.0.0.1:${port}:8096" "$image" >/dev/null
  note "started $name ($image) on 127.0.0.1:${port}"
}

start_tindarr() {
  local name="$1" port="$2" data_dir="$3"
  if [ "$IMAGE" = "source" ]; then
    mkdir -p "$data_dir"
    [ -d "${repo_root}/web/dist" ] || fail "web/dist is missing: run 'npm run build' in web/"
    TINDARR_DATA_DIR="$data_dir" TINDARR_WEB_DIR="${repo_root}/web/dist" \
      TINDARR_HOST=127.0.0.1 TINDARR_PORT="$port" TINDARR_LOG_LEVEL=INFO \
      "${UV:-uv}" run --project "${repo_root}/server" --quiet tindarr serve \
      >"${STATE_DIR}/${name}.log" 2>&1 &
    echo $! >"${STATE_DIR}/${name}.pid"
  else
    docker volume create "$name" >/dev/null
    docker run -d --name "$name" --network host \
      --read-only --tmpfs /tmp:size=64m \
      --cap-drop ALL --security-opt no-new-privileges:true \
      --memory 512m --pids-limit 256 \
      -e TINDARR_HOST=127.0.0.1 -e TINDARR_PORT="$port" -e TINDARR_LOG_LEVEL=INFO \
      -v "$name:/data" "$IMAGE" >/dev/null
  fi
  wait_for "http://127.0.0.1:${port}/healthz" "the Tindarr server for $name"
}

read_setup_code() { # read_setup_code <container name> <source data dir>
  local name="$1" data_dir="$2" code="" deadline=$((SECONDS + 60))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if [ "$IMAGE" = "source" ]; then
      code="$(cat "${data_dir}/setup-code" 2>/dev/null || true)"
    else
      code="$(docker exec "$name" cat /data/setup-code 2>/dev/null || true)"
    fi
    [ -n "$code" ] && { echo "$code"; return 0; }
    sleep 1
  done
  fail "$name never wrote its setup code"
}

up() {
  command -v docker >/dev/null || fail "docker is required"
  mkdir -p "$STATE_DIR"
  local admin_password plain_password
  admin_password="Aa1-$(openssl rand -hex 16)"
  plain_password="Bb2-$(openssl rand -hex 16)"

  : >"$ENV_FILE"
  chmod 600 "$ENV_FILE"
  {
    echo "E2E_ADMIN_USER=${ADMIN_USER}"
    echo "E2E_ADMIN_PASSWORD=${admin_password}"
    echo "E2E_USER=${PLAIN_USER}"
    echo "E2E_USER_PASSWORD=${plain_password}"
  } >>"$ENV_FILE"

  local row
  while read -r row; do
    [ -n "$row" ] || continue
    local target kind image media_port console_port
    target="$(field "$row" 1)"; kind="$(field "$row" 2)"; image="$(field "$row" 3)"
    media_port="$(field "$row" 4)"; console_port="$(field "$row" 5)"
    start_media_server "${PREFIX}-media-${target}" "$kind" "$image" "$media_port"
  done < <(selected_targets "$@")

  while read -r row; do
    [ -n "$row" ] || continue
    local target kind media_port console_port media_url seeded server_name data_dir
    target="$(field "$row" 1)"; kind="$(field "$row" 2)"
    media_port="$(field "$row" 4)"; console_port="$(field "$row" 5)"
    media_url="http://127.0.0.1:${media_port}"
    note "seeding ${target} (${kind}) at ${media_url}"
    # The passwords go through the environment, not argv: a command line is readable
    # by every process on the machine for as long as the command runs.
    seeded="$(SEED_ADMIN_PASSWORD="$admin_password" SEED_USER_PASSWORD="$plain_password" \
      python3 "${repo_root}/.github/scripts/seed-media-server.py" \
      --kind "$kind" --url "$media_url" \
      --admin "$ADMIN_USER" --user "$PLAIN_USER" \
      --timeout "$READY_TIMEOUT")"
    local api_key
    api_key="$(echo "$seeded" | sed -n 's/^MEDIA_API_KEY=//p')"
    [ -n "$api_key" ] || fail "no API key came back for $target"

    server_name="${PREFIX}-server-${target}"
    data_dir="${STATE_DIR}/${target}-data"
    start_tindarr "$server_name" "$console_port" "$data_dir"
    local code
    code="$(read_setup_code "$server_name" "$data_dir")"

    local upper
    upper="$(echo "$target" | tr '[:lower:]' '[:upper:]')"
    {
      echo "E2E_${upper}_URL=http://127.0.0.1:${console_port}"
      echo "E2E_${upper}_KIND=${kind}"
      echo "E2E_${upper}_MEDIA_URL=${media_url}"
      echo "E2E_${upper}_API_KEY=${api_key}"
      echo "E2E_${upper}_SETUP_CODE=${code}"
    } >>"$ENV_FILE"
    note "${target}: console on http://127.0.0.1:${console_port}"
  done < <(selected_targets "$@")

  note "wrote $ENV_FILE"
}

# ------------------------------------------------------------------- inspection

tindarr_logs() { # tindarr_logs <target>
  local name="${PREFIX}-server-$1"
  if [ "$IMAGE" = "source" ]; then
    cat "${STATE_DIR}/${name}.log" 2>/dev/null || true
  else
    docker logs "$name" 2>&1 || true
  fi
}

check_logs() {
  local row failed=0
  while read -r row; do
    [ -n "$row" ] || continue
    local target
    target="$(field "$row" 1)"
    if ! tindarr_logs "$target" | python3 -c '
import json, sys

# The scenario is a happy path: nothing it does should make the server log an error.
# Anything that is not valid JSON is a traceback or a crash on stderr, which counts too.
bad = []
for line in sys.stdin.read().splitlines():
    if not line.strip():
        continue
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        bad.append(line)
        continue
    if entry.get("level") in ("ERROR", "CRITICAL"):
        bad.append(line)
if bad:
    print("\n".join(bad[:40]))
    sys.exit(1)
'; then
      echo "::error::e2e-stack: the Tindarr server for ${target} logged errors (above)"
      failed=1
    fi
  done < <(selected_targets)
  [ "$failed" -eq 0 ] || exit 1
  note "no error in any Tindarr server log"
}

collect_logs() {
  local out="${1:-${repo_root}/e2e-logs}"
  mkdir -p "$out"
  local row
  while read -r row; do
    [ -n "$row" ] || continue
    local target
    target="$(field "$row" 1)"
    tindarr_logs "$target" >"${out}/tindarr-${target}.log"
    docker logs "${PREFIX}-media-${target}" >"${out}/media-${target}.log" 2>&1 || true
  done < <(selected_targets)
  note "logs written to $out"
}

down() {
  local name
  if [ -d "$STATE_DIR" ]; then
    for name in "$STATE_DIR"/*.pid; do
      [ -e "$name" ] || continue
      kill "$(cat "$name")" 2>/dev/null || true
      rm -f "$name"
    done
  fi
  for name in $(docker ps -aq --filter "name=^${PREFIX}-" 2>/dev/null); do
    docker rm -f -v "$name" >/dev/null 2>&1 || true
  done
  for name in $(docker volume ls -q --filter "name=^${PREFIX}-server-" 2>/dev/null); do
    docker volume rm -f "$name" >/dev/null 2>&1 || true
  done
  rm -rf "$STATE_DIR"
  rm -f "$ENV_FILE"
  note "stack removed"
}

command="${1:-}"
shift || true
case "$command" in
  up) up "$@" ;;
  check-logs) check_logs ;;
  logs) collect_logs "$@" ;;
  down) down ;;
  *) fail "usage: $0 <up|check-logs|logs|down> [target …]" ;;
esac
