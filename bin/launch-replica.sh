#!/bin/bash
# launch-replica.sh <port> [server-name]
#
# Launch an extra stateless mcp-openapi-proxy replica for <server-name>
# (default gpt-terminal-plus) on an alternate port. Reuses the same env
# expansion as bin/launch-server.sh (servers.json + gateway .env) but binds a
# caller-supplied port, so nginx can front a pool of replicas (issue #34).
#
# Why replicas: mcp-openapi-proxy runs a single uvicorn worker with a blocking
# event loop — one process is an availability ceiling under concurrent tool
# calls. The proxy is stateless (no Mcp-Session-Id / sticky routing), so we
# scale horizontally here instead of modifying the vendored package.
set -euo pipefail

PORT="${1:?usage: launch-replica.sh <port> [server-name]}"
NAME="${2:-gpt-terminal-plus}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXPANDER="$ROOT/bin/expand_env.py"
MANIFEST="$ROOT/servers.json"

[[ -f "$MANIFEST" ]] || { echo "launch-replica: $MANIFEST not found" >&2; exit 1; }
[[ -x "$EXPANDER" ]] || { echo "launch-replica: $EXPANDER not found" >&2; exit 1; }

# Same credential aliases the primary launcher sets.
export TERMINAL_API_KEY="${TERMINAL_API_KEY:-${API_TOKEN:-}}"
export GPT_TERMINAL_PLUS_URL="${GPT_TERMINAL_PLUS_URL:-http://localhost:${GPT_TERMINAL_PLUS_PORT:-3100}}"

read -r SPEC < <(python3 "$EXPANDER" --manifest "$MANIFEST" --server "$NAME" --field spec_url)
mapfile -t ENVPAIRS < <(python3 "$EXPANDER" --manifest "$MANIFEST" --server "$NAME" --env)

ENGINE_SRC="${MCP_ENGINE_SRC:-/home/chatgpt/mcp-openapi-proxy}"
PROXY_ENTRY="$ROOT/bin/mcp_openapi_proxy_gw.py"

exec env -i \
  HOME="$HOME" \
  PATH="${PATH}" \
  OPENAPI_SPEC_URL="$SPEC" \
  MCP_SERVER_NAME="$NAME" \
  MCP_TRANSPORT=streamable-http \
  MCP_HOST=127.0.0.1 \
  MCP_PORT="$PORT" \
  MCP_PATH=/mcp \
  MCP_JSON_RESPONSE=true \
  MCP_ALLOWED_HOSTS='*' \
  MCP_LOG_RAW_COMMANDS="${MCP_LOG_RAW_COMMANDS:-1}" \
  ${ENVPAIRS[@]+"${ENVPAIRS[@]}"} \
  uv run --directory "$ENGINE_SRC" --python 3.12 -- python "$PROXY_ENTRY"