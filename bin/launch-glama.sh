#!/bin/bash
# Glama MCP bridge for Letta Cloud — spec-only, NO secrets. Fronted by the
# bearer-gated nginx vhost on 443; :8765 is blocked externally by the host firewall.
ENGINE_SRC="${MCP_OPENAPI_PROXY_SRC:-/home/chatgpt/mcp-openapi-proxy}"
if [ "${MCP_GATEWAY_HTTP:-native}" = "supergateway" ]; then
  exec env -i HOME=/home/chatgpt PATH="/home/chatgpt/.local/bin:/usr/local/bin:/usr/bin:/bin" \
    OPENAPI_SPEC_URL=https://glama.ai/api/mcp/openapi.json \
    npx -y supergateway --stdio "uvx --python 3.12 --from '${ENGINE_SRC}' --with 'mcp>=2,<3' mcp-openapi-proxy" --outputTransport streamableHttp --port 8765 --logLevel info
fi
exec env -i HOME=/home/chatgpt PATH="/home/chatgpt/.local/bin:/usr/local/bin:/usr/bin:/bin" \
  OPENAPI_SPEC_URL=https://glama.ai/api/mcp/openapi.json \
  MCP_SERVER_NAME=glama \
  MCP_TRANSPORT=streamable-http \
  MCP_HOST=127.0.0.1 \
  MCP_PORT=8765 \
  MCP_PATH=/mcp \
  MCP_JSON_RESPONSE=true \
  MCP_ALLOWED_HOSTS='*' \
  uv run --directory "$ENGINE_SRC" --python 3.12 mcp-openapi-proxy
