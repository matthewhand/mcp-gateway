#!/bin/bash
# wordpress-dev only: same as launch-server.sh but stdio is a persistent venv
# so each MCP session does not pay uvx cold-start (2-20s / DeadlineExceeded).
set -euo pipefail
NAME=wordpress-dev
MANIFEST=/home/chatgpt/mcp-gateway/servers.json
VENV=/home/chatgpt/mcp-gateway/venvs/wordpress-dev
PROXY=/home/chatgpt/mcp-gateway/bin/mcp_openapi_proxy_gw.py
log() { echo "[launch-wordpress-dev] $*"; }

load_env_file() {
  local f="$1"
  [ -f "$f" ] || return 0
  set -a
  # shellcheck source=/dev/null
  source "$f"
  set +a
}
load_env_file /home/chatgpt/.config/swarm/.env
load_env_file /home/chatgpt/mcp-gateway/.env

read -r PORT SPEC < <(python3 -c "
import json
d=json.load(open('$MANIFEST'))['$NAME']
print(d['port'], d['spec_url'])
")
mapfile -t ENVPAIRS < <(python3 -c "
import json,sys
sys.path.insert(0, '/home/chatgpt/mcp-gateway/bin')
from expand_env import expand
content=expand(open('$MANIFEST').read())
d=json.loads(content)['$NAME'].get('env',{})
for k,v in d.items():
    print(f'{k}={v}')
")

if [ ! -x "$VENV/bin/python" ]; then
  log "ERROR: venv python missing $VENV/bin/python"
  exit 1
fi
case "$SPEC" in
  file://*)
    path="${SPEC#file://}"
    bytes=$(wc -c <"$path" | tr -d ' ')
    log "OPENAPI_SPEC file path=$path size_bytes=$bytes"
    ;;
  *) log "OPENAPI_SPEC url=$SPEC" ;;
esac

log "starting port=$PORT venv=$VENV entry=$PROXY"
ENGINE_SRC="${MCP_OPENAPI_PROXY_SRC:-/home/chatgpt/mcp-openapi-proxy}"
# Persistent venv kept for emergency fallback; native 0.4.0 uses the
# mcp-openapi-proxy project venv (mcp 2.1.1, no session, no uvx cold start).
if [ "${MCP_GATEWAY_HTTP:-native}" = "supergateway" ]; then
  exec env -i HOME=/home/chatgpt PATH="/home/chatgpt/.local/bin:/usr/local/bin:/usr/bin:/bin" \
    OPENAPI_SPEC_URL="$SPEC" ${ENVPAIRS[@]+"${ENVPAIRS[@]}"} \
    npx -y supergateway --stdio "$VENV/bin/python ${PROXY}" \
    --outputTransport streamableHttp --port "$PORT" --logLevel info
fi
exec env -i HOME=/home/chatgpt PATH="/home/chatgpt/.local/bin:/usr/local/bin:/usr/bin:/bin" \
  OPENAPI_SPEC_URL="$SPEC" \
  MCP_SERVER_NAME="$NAME" \
  MCP_TRANSPORT=streamable-http \
  MCP_HOST=127.0.0.1 \
  MCP_PORT="$PORT" \
  MCP_PATH=/mcp \
  MCP_JSON_RESPONSE=true \
  MCP_ALLOWED_HOSTS='*' \
  ${ENVPAIRS[@]+"${ENVPAIRS[@]}"} \
  uv run --directory "$ENGINE_SRC" --python 3.12 -- python "${PROXY}"
