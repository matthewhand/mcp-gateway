#!/bin/bash
# launch-server.sh <name>
# Launch a SPEC-ONLY (no secrets) supergateway for one mcp-openapi-proxy server,
# reading port/spec/non-secret-env from servers.json. Fronted by the bearer/OAuth
# nginx vhost; example ports are firewalled from the internet.
#
# Stdout/stderr go to ~/.state/mcp-gateway/servers.log (systemd unit).
set -euo pipefail
NAME="${1:?usage: launch-server.sh <name>}"
MANIFEST=/home/chatgpt/mcp-gateway/servers.json
# Shared ${VAR} / ${VAR:-default} expander (see bin/expand_env.py). Resolved from
# the manifest's directory because ROOT is only defined further down this script.
EXPANDER="$(dirname "$MANIFEST")/bin/expand_env.py"
# Soft / hard thresholds for OpenAPI fetch preflight (seconds).
# Clients and proxies often give up between 30–60s on initialize/tools/list.
SLOW_WARN_SEC="${MCP_SPEC_SLOW_WARN_SEC:-10}"
SLOW_CRIT_SEC="${MCP_SPEC_SLOW_CRIT_SEC:-30}"

log() { echo "[launch-server ${NAME}] $*"; }

# Load secrets from env files (never commit these). Gateway .env is primary;
# swarm/.env is optional fallback for shared keys under different names.
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
# Common aliases (swarm/legacy names → servers.json ${VAR} names)
export FLYIO_API_KEY="${FLYIO_API_KEY:-${FLY_API_KEY:-}}"
export VIRUSTOTAL_API_KEY="${VIRUSTOTAL_API_KEY:-${VT_API_KEY:-}}"
export WOLFRAMALPHA_API_KEY="${WOLFRAMALPHA_API_KEY:-${WOLFRAM_LLM_APP_ID:-}}"
export OPEN_SWARM_API_KEY="${OPEN_SWARM_API_KEY:-${API_AUTH_TOKEN:-${SWARM_API_KEY:-}}}"
export TERMINAL_API_KEY="${TERMINAL_API_KEY:-${API_TOKEN:-}}"
export GITHUB_API_KEY="${GITHUB_API_KEY:-${GITHUB_PERSONAL_ACCESS_TOKEN:-}}"
# gpt-terminal-plus upstream (servers.json keeps it as a ${GPT_TERMINAL_PLUS_URL:-…}
# ref). Setting only GPT_TERMINAL_PLUS_PORT here is enough to retarget the bridge;
# the default keeps today's http://localhost:3100. Docs: README / docs/RUNBOOK.md.
export GPT_TERMINAL_PLUS_URL="${GPT_TERMINAL_PLUS_URL:-http://localhost:${GPT_TERMINAL_PLUS_PORT:-3100}}"

read -r PORT SPEC < <(python3 "$EXPANDER" --manifest "$MANIFEST" --server "$NAME" \
  --field port --field spec_url)
mapfile -t ENVPAIRS < <(python3 "$EXPANDER" --manifest "$MANIFEST" --server "$NAME" --env)

# ---------------------------------------------------------------------------
# Preflight: time the OpenAPI fetch mcp-openapi-proxy will also perform.
# A 70s NetBox /api/schema/ is "healthy" but clients will not wait that long.
# ---------------------------------------------------------------------------
preflight_openapi_spec() {
  local url="$1"
  case "$url" in
    file://*)
      local path="${url#file://}"
      if [ -f "$path" ]; then
        local bytes
        bytes=$(wc -c <"$path" | tr -d ' ')
        log "OPENAPI_SPEC file path=$path size_bytes=$bytes"
        # ~1MB+ local YAML can still make first tools/list slow on low-memory hosts
        if [ "${bytes:-0}" -ge 1000000 ]; then
          log "WARN: local OpenAPI is large (${bytes} bytes). First initialize/tools/list may be slow; prefer a minified/whitelisted spec if clients time out."
        fi
      else
        log "WARN: OPENAPI_SPEC file missing path=$path (proxy will fail to load tools)"
      fi
      return 0
      ;;
    http://*|https://*)
      ;;
    *)
      log "OPENAPI_SPEC url=$url (no preflight)"
      return 0
      ;;
  esac

  local max_t="${MCP_SPEC_PREFLIGHT_MAX_SEC:-120}"
  local out http elapsed
  # curl: write body to /dev/null, emit "HTTP_CODE TIME_TOTAL SIZE"
  out=$(curl -sS -L -o /dev/null \
    -w '%{http_code} %{time_total} %{size_download}' \
    --connect-timeout 5 \
    --max-time "$max_t" \
    "$url" 2>&1) || true
  # shellcheck disable=SC2086
  set -- $out
  http="${1:-000}"
  elapsed="${2:-0}"
  local size="${3:-0}"
  # normalize elapsed to 2 decimals via awk
  elapsed=$(awk -v t="$elapsed" 'BEGIN{printf "%.2f", t+0}')

  log "OPENAPI_SPEC preflight port=$PORT url=$url http=$http elapsed_sec=$elapsed size_bytes=$size"

  if [ "$http" != "200" ] && [ "$http" != "301" ] && [ "$http" != "302" ]; then
    log "WARN: OpenAPI preflight HTTP $http — proxy may start with zero tools."
  fi

  # Compare as numbers
  if awk -v e="$elapsed" -v w="$SLOW_WARN_SEC" 'BEGIN{exit !(e+0 >= w+0)}'; then
    log "WARN: OpenAPI fetch took ${elapsed}s (>= ${SLOW_WARN_SEC}s). MCP clients often time out at 30–60s on initialize/tools/list while the proxy is still loading the spec — clients may hang or see empty tools. Prefer caching to file:// (see specs/) or a smaller whitelist-generated schema."
  fi
  if awk -v e="$elapsed" -v c="$SLOW_CRIT_SEC" 'BEGIN{exit !(e+0 >= c+0)}'; then
    log "CRIT: OpenAPI fetch took ${elapsed}s (>= ${SLOW_CRIT_SEC}s). Clients almost certainly will not wait this long. Cache the schema locally before clients connect."
  fi
}

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Gateway entry adds API_AUTH_TYPE=query injection (stock proxy is header-only).
PROXY_ENTRY="${ROOT}/bin/mcp_openapi_proxy_gw.py"

# Both exec paths below use `env -i`, which discards the inherited environment.
# Opt-in raw shell-command audit logging (MCP_LOG_RAW_COMMANDS=1) therefore has to
# be re-exported explicitly or the proxy never sees it. WARNING: raw commands are
# logged verbatim and may contain credentials -- enable only when you want that
# in ~/.state/mcp-gateway/servers.log.
RAW_LOG_ENV=()
for _rawvar in MCP_LOG_RAW_COMMANDS MCP_LOG_RAW_COMMAND_TOOLS; do
  if [ -n "${!_rawvar:-}" ]; then
    RAW_LOG_ENV+=("$_rawvar=${!_rawvar}")
  fi
done
if [ "${#RAW_LOG_ENV[@]}" -gt 0 ]; then
  log "raw command logging enabled: ${RAW_LOG_ENV[*]}"
fi

# box :8810 is key-gated. Restart=always — do not exit (that flaps); hold
# without binding when BOX_API_KEY is empty/expired. Status marks UNHEALTHY.
if [ "$NAME" = box ]; then
  if ! python3 "$ROOT/bin/box_key.py" --check --root "$ROOT" --manifest "$MANIFEST"; then
    log "BOX_API_KEY empty/expired — skip launch (short-lived Box token; refresh BOX_API_KEY)"
    exec sleep infinity
  fi
fi

log "starting port=$PORT OPENAPI_SPEC_URL=$SPEC entry=$PROXY_ENTRY"
preflight_openapi_spec "$SPEC"

ENGINE_SRC="${MCP_OPENAPI_PROXY_SRC:-/home/chatgpt/mcp-openapi-proxy}"
# Native 2026-07-28 Streamable HTTP is stateless (no Mcp-Session-Id, no sticky).
# Set MCP_GATEWAY_HTTP=supergateway to restore the 0.3.4 stateful wrapper.
if [ "${MCP_GATEWAY_HTTP:-native}" = "supergateway" ]; then
  exec env -i HOME=/home/chatgpt PATH="/home/chatgpt/.local/bin:/usr/local/bin:/usr/bin:/bin" \
    OPENAPI_SPEC_URL="$SPEC" MCP_SERVER_NAME="$NAME" ${ENVPAIRS[@]+"${ENVPAIRS[@]}"} \
    ${RAW_LOG_ENV[@]+"${RAW_LOG_ENV[@]}"} \
    npx -y supergateway --stdio "uvx --python 3.12 --from '${ENGINE_SRC}' --with 'mcp>=2,<3' -- python ${PROXY_ENTRY}" \
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
  ${RAW_LOG_ENV[@]+"${RAW_LOG_ENV[@]}"} \
  uv run --directory "$ENGINE_SRC" --python 3.12 -- python "${PROXY_ENTRY}"
