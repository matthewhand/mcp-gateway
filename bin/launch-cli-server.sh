#!/bin/bash
# launch-cli-server.sh <name>
# Starts CLI HTTP bridge (cli_bridge.py) + supergateway/mcp-openapi-proxy under one
# supervised process tree. Holds a per-name flock so dual system/user units cannot race.
set -euo pipefail
NAME="${1:?usage: launch-cli-server.sh <name>}"
ROOT=/home/chatgpt/mcp-gateway
MANIFEST="$ROOT/servers.json"
STATE="${MCP_GATEWAY_STATE:-$HOME/.state/mcp-gateway}"
LOCK_DIR="$STATE/locks"
mkdir -p "$LOCK_DIR"

load_env_file() {
  local f="$1"
  [ -f "$f" ] || return 0
  set -a
  # shellcheck source=/dev/null
  source "$f"
  set +a
}
load_env_file "$HOME/.config/swarm/.env"
load_env_file "$ROOT/.env"
export FLYIO_API_KEY="${FLYIO_API_KEY:-${FLY_API_KEY:-}}"
export VIRUSTOTAL_API_KEY="${VIRUSTOTAL_API_KEY:-${VT_API_KEY:-}}"
export WOLFRAMALPHA_API_KEY="${WOLFRAMALPHA_API_KEY:-${WOLFRAM_LLM_APP_ID:-}}"
export OPEN_SWARM_API_KEY="${OPEN_SWARM_API_KEY:-${API_AUTH_TOKEN:-${SWARM_API_KEY:-}}}"
export TERMINAL_API_KEY="${TERMINAL_API_KEY:-${API_TOKEN:-}}"
export GITHUB_API_KEY="${GITHUB_API_KEY:-${GITHUB_PERSONAL_ACCESS_TOKEN:-}}"

read -r PORT CLI_PORT CLI_CONFIG SPEC < <(python3 -c "
import json,sys
sys.path.insert(0, '$ROOT/bin')
from expand_env import expand
with open('$MANIFEST') as f:
    content = expand(f.read())
d=json.loads(content)['$NAME']
print(d['port'], d.get('cli_port', 3860), d['cli_config'], d['spec_url'])
")

mapfile -t ENVPAIRS < <(python3 -c "
import json,sys
sys.path.insert(0, '$ROOT/bin')
from expand_env import expand
with open('$MANIFEST') as f:
    content = expand(f.read())
d=json.loads(content)['$NAME'].get('env',{})
for k,v in d.items(): print(f'{k}={v}')
")

# Same lock as prepare-cli-server.sh (prepare already holds it during ExecStartPre;
# we re-acquire for the lifetime of this process so another unit cannot prepare mid-run).
exec 9>"$LOCK_DIR/${NAME}.lock"
if ! flock -w 90 9; then
  echo "[launch-cli-server ${NAME}] ERROR: could not acquire lock" >&2
  exit 1
fi

port_listeners() {
  # print pids listening on port (best-effort)
  ss -tlnp 2>/dev/null | grep ":${1}\\b" | grep -oP 'pid=\\K[0-9]+' | sort -u
}

require_port_free() {
  local port="$1"
  local pids
  for _ in $(seq 1 40); do
    pids=$(port_listeners "$port" || true)
    if [ -z "${pids//[$'\n']/}" ]; then
      return 0
    fi
    sleep 0.25
  done
  echo "[launch-cli-server ${NAME}] ERROR: port :$port still busy (pids: $(port_listeners "$port" | tr '\n' ' '))" >&2
  exit 1
}

# Ports must be free — do NOT treat leftover "friendly" listeners as ours.
require_port_free "$PORT"
require_port_free "$CLI_PORT"

BRIDGE_PID=""
SG_PID=""
cleaning=0

cleanup() {
  if [ "$cleaning" = 1 ]; then
    return 0
  fi
  cleaning=1
  # Stop supergateway first (may be holding client sessions), then bridge.
  if [ -n "${SG_PID:-}" ] && kill -0 "$SG_PID" 2>/dev/null; then
    kill -TERM "$SG_PID" 2>/dev/null || true
  fi
  if [ -n "${BRIDGE_PID:-}" ] && kill -0 "$BRIDGE_PID" 2>/dev/null; then
    kill -TERM "$BRIDGE_PID" 2>/dev/null || true
  fi
  # Give them a moment, then escalate
  sleep 0.5
  if [ -n "${SG_PID:-}" ] && kill -0 "$SG_PID" 2>/dev/null; then
    kill -KILL "$SG_PID" 2>/dev/null || true
  fi
  if [ -n "${BRIDGE_PID:-}" ] && kill -0 "$BRIDGE_PID" 2>/dev/null; then
    kill -KILL "$BRIDGE_PID" 2>/dev/null || true
  fi
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

python3 "$ROOT/bin/cli_bridge.py" serve --config "$ROOT/$CLI_CONFIG" --port "$CLI_PORT" &
BRIDGE_PID=$!

SLOW_WARN_SEC="${MCP_SPEC_SLOW_WARN_SEC:-10}"
SLOW_CRIT_SEC="${MCP_SPEC_SLOW_CRIT_SEC:-30}"
_cli_wait_start=$(date +%s)
_cli_ready=0
for _ in $(seq 1 90); do
  if ! kill -0 "$BRIDGE_PID" 2>/dev/null; then
    echo "[launch-cli-server ${NAME}] ERROR: cli bridge exited before ready on :$CLI_PORT" >&2
    wait "$BRIDGE_PID" 2>/dev/null || true
    exit 1
  fi
  if curl -sf "http://127.0.0.1:${CLI_PORT}/openapi.json" >/dev/null; then
    # Prefer proving our bridge owns the socket. If ss omits pids (permissions),
    # accept readiness when our BRIDGE_PID is still alive and openapi answers —
    # prepare already required the port free before we bound.
    listeners=$(port_listeners "$CLI_PORT" || true)
    if [ -n "$listeners" ]; then
      owned=0
      while read -r lpid; do
        [ -z "$lpid" ] && continue
        if [ "$lpid" = "$BRIDGE_PID" ]; then
          owned=1
          break
        fi
        # ss may show a worker; accept if parent is our bridge
        ppid=$(python3 -c "import pathlib; s=pathlib.Path('/proc/$lpid/stat').read_text(); print(s.split(')',1)[-1].split()[1])" 2>/dev/null || true)
        if [ "$ppid" = "$BRIDGE_PID" ]; then
          owned=1
          break
        fi
      done <<< "$listeners"
      if [ "$owned" != 1 ]; then
        echo "[launch-cli-server ${NAME}] ERROR: :$CLI_PORT openapi up but not owned by bridge pid=$BRIDGE_PID (listeners: $listeners)" >&2
        exit 1
      fi
    fi
    _cli_ready=1
    break
  fi
  sleep 0.25
done
_cli_wait_sec=$(( $(date +%s) - _cli_wait_start ))
if [ "$_cli_ready" != 1 ]; then
  echo "[launch-cli-server ${NAME}] ERROR: cli bridge failed to start on :$CLI_PORT after ${_cli_wait_sec}s" >&2
  exit 1
fi
echo "[launch-cli-server ${NAME}] cli openapi ready port=$CLI_PORT wait_sec=${_cli_wait_sec} pid=$BRIDGE_PID"
if [ "$_cli_wait_sec" -ge "$SLOW_WARN_SEC" ]; then
  echo "[launch-cli-server ${NAME}] WARN: CLI openapi took ${_cli_wait_sec}s (>= ${SLOW_WARN_SEC}s)." >&2
fi
if [ "$_cli_wait_sec" -ge "$SLOW_CRIT_SEC" ]; then
  echo "[launch-cli-server ${NAME}] CRIT: CLI openapi took ${_cli_wait_sec}s (>= ${SLOW_CRIT_SEC}s)." >&2
fi

# Require MCP port still free right before bind
require_port_free "$PORT"

# Do NOT exec-away: keep this shell as MainPID so both children stay supervised.
# shellcheck disable=SC2086
ENGINE_SRC="${MCP_OPENAPI_PROXY_SRC:-/home/chatgpt/mcp-openapi-proxy}"
if [ "${MCP_GATEWAY_HTTP:-native}" = "supergateway" ]; then
  env -i HOME=/home/chatgpt PATH="/home/chatgpt/.local/bin:/usr/local/bin:/usr/bin:/bin" \
    OPENAPI_SPEC_URL="$SPEC" TOOL_NAME_USE_OPERATION_ID=true ${ENVPAIRS[@]+"${ENVPAIRS[@]}"} \
    npx -y supergateway --stdio "uvx --python 3.12 --from '${ENGINE_SRC}' --with 'mcp>=2,<3' -- python $ROOT/bin/mcp_openapi_proxy_cli.py" \
    --outputTransport streamableHttp --port "$PORT" --logLevel info &
  SG_PID=$!
else
  env -i HOME=/home/chatgpt PATH="/home/chatgpt/.local/bin:/usr/local/bin:/usr/bin:/bin" \
    OPENAPI_SPEC_URL="$SPEC" TOOL_NAME_USE_OPERATION_ID=true \
    MCP_SERVER_NAME="$NAME" \
    MCP_TRANSPORT=streamable-http \
    MCP_HOST=127.0.0.1 \
    MCP_PORT="$PORT" \
    MCP_PATH=/mcp \
    MCP_JSON_RESPONSE=true \
    MCP_ALLOWED_HOSTS='*' \
    ${ENVPAIRS[@]+"${ENVPAIRS[@]}"} \
    uv run --directory "$ENGINE_SRC" --python 3.12 -- python "$ROOT/bin/mcp_openapi_proxy_cli.py" &
  SG_PID=$!
fi

# Wait until MCP port is listening (child node may bind after npm wrapper starts)
_mcp_ready=0
for _ in $(seq 1 60); do
  if ! kill -0 "$SG_PID" 2>/dev/null; then
    echo "[launch-cli-server ${NAME}] ERROR: supergateway exited before binding :$PORT" >&2
    wait "$SG_PID" 2>/dev/null || true
    exit 1
  fi
  if ! kill -0 "$BRIDGE_PID" 2>/dev/null; then
    echo "[launch-cli-server ${NAME}] ERROR: cli bridge died while starting MCP :$PORT" >&2
    exit 1
  fi
  if ss -tln 2>/dev/null | grep -qE ":${PORT}\\b"; then
    _mcp_ready=1
    break
  fi
  sleep 0.25
done
if [ "$_mcp_ready" != 1 ]; then
  echo "[launch-cli-server ${NAME}] ERROR: MCP port :$PORT not listening after start" >&2
  exit 1
fi
echo "[launch-cli-server ${NAME}] mcp listening port=$PORT sg_pid=$SG_PID"

# Stay up until either child exits; then fail so systemd Restart=always can recover cleanly.
while true; do
  if ! kill -0 "$BRIDGE_PID" 2>/dev/null; then
    echo "[launch-cli-server ${NAME}] ERROR: cli bridge exited" >&2
    wait "$BRIDGE_PID" 2>/dev/null || true
    exit 1
  fi
  if ! kill -0 "$SG_PID" 2>/dev/null; then
    echo "[launch-cli-server ${NAME}] ERROR: supergateway exited" >&2
    wait "$SG_PID" 2>/dev/null || true
    exit 1
  fi
  sleep 2
done
