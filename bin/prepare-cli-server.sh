#!/bin/bash
# prepare-cli-server.sh <name>
# Free this unit's MCP + CLI-bridge ports before ExecStart (avoid EADDRINUSE races).
# Uses flock so concurrent prepare/launch (system vs user, or watchdog) cannot interleave.
set -euo pipefail
NAME="${1:?usage: prepare-cli-server.sh <name>}"
ROOT=/home/chatgpt/mcp-gateway
MANIFEST="$ROOT/servers.json"
STATE="${MCP_GATEWAY_STATE:-$HOME/.state/mcp-gateway}"
LOCK_DIR="$STATE/locks"
mkdir -p "$LOCK_DIR"

read -r PORT CLI_PORT CLI_CONFIG < <(python3 -c "
import json, sys
sys.path.insert(0, '$ROOT/bin')
from expand_env import expand
with open('$MANIFEST') as f:
    content = expand(f.read())
d = json.loads(content)['$NAME']
print(d['port'], d.get('cli_port', 3860), d.get('cli_config', ''))
")

# Exclusive per-server lock (shared with launch-cli-server.sh)
exec 9>"$LOCK_DIR/${NAME}.lock"
if ! flock -w 90 9; then
  echo "prepare-cli-server: could not acquire lock for ${NAME}" >&2
  exit 1
fi

port_busy() {
  ss -tln 2>/dev/null | grep -qE ":${1}\\b"
}

kill_port() {
  local port="$1" sig="$2"
  fuser -k -"${sig}" "${port}/tcp" 2>/dev/null || true
}

# Also stop stray stacks that may not be listening yet but hold the same role.
kill_strays() {
  local pid cmd
  for pid in $(ps -eo pid=); do
    pid="${pid// /}"
    [ -n "$pid" ] || continue
    cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)
    [ -n "$cmd" ] || continue
    # Match this server's bridge config and/or MCP port
    if [[ -n "$CLI_CONFIG" && "$cmd" == *"cli_bridge.py"* && "$cmd" == *"$CLI_CONFIG"* ]]; then
      kill -TERM "$pid" 2>/dev/null || true
      continue
    fi
    if [[ "$cmd" == *"--port ${PORT}"* && "$cmd" == *"supergateway"* ]]; then
      kill -TERM "$pid" 2>/dev/null || true
      continue
    fi
    if [[ "$cmd" == *"port ${PORT}"* && "$cmd" == *"supergateway"* ]]; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
}

kill_strays
for sig in TERM KILL; do
  kill_port "$PORT" "$sig"
  kill_port "$CLI_PORT" "$sig"
  if [ "$sig" = TERM ]; then
    sleep 0.3
    kill_strays
  fi
  for _ in $(seq 1 20); do
    if ! port_busy "$PORT" && ! port_busy "$CLI_PORT"; then
      # brief settle so SO_REUSEADDR races settle
      sleep 0.2
      if ! port_busy "$PORT" && ! port_busy "$CLI_PORT"; then
        exit 0
      fi
    fi
    sleep 0.2
  done
done

echo "prepare-cli-server: ports :${PORT}/:${CLI_PORT} still busy for ${NAME}" >&2
ss -tlnp 2>/dev/null | grep -E ":${PORT}\\b|:${CLI_PORT}\\b" || true
exit 1
