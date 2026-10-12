#!/usr/bin/env bash
# acpx-wrap.sh — launch acpx with stable PATH + auth + LiteLLM Fly env for MCP/systemd.
# Does not print secrets. Used as command[0] in configs/acpx.json.
set -euo pipefail

export HOME="${HOME:-/home/chatgpt}"
export PATH="/home/chatgpt/.litellm/scripts:/home/chatgpt/.litellm/scripts/acp:/home/chatgpt/.grok/bin:/home/chatgpt/.npm-global/bin:/home/chatgpt/.opencode/bin:/home/chatgpt/.local/bin:/usr/local/bin:/usr/bin:/bin"

# --- LiteLLM Fly (coding agents: qwen/gemini/opencode/kilocode/codex ACP wrappers) ---
# grok-build ignores these and uses its own subscription auth.
_load_env_file() {
  local f="$1"
  [[ -f "$f" ]] || return 0
  set -a
  # shellcheck disable=SC1090
  source "$f"
  set +a
}
_load_env_file "${HOME}/.litellm/.env"
_load_env_file "${HOME}/.litellm/.env.fly"
_load_env_file "${HOME}/mcp-gateway/.env"

: "${LITELLM_BASE_URL:=https://open-litellm.fly.dev}"
# Normalize: accept callers exporting BASE_URL with a trailing /v1 (avoids /v1/v1 → 404)
LITELLM_BASE_URL="${LITELLM_BASE_URL%/}"
LITELLM_BASE_URL="${LITELLM_BASE_URL%/v1}"
: "${LITELLM_OPENAI_BASE:=${LITELLM_BASE_URL}/v1}"
if [[ -z "${LITELLM_API_KEY:-}" && -n "${LITELLM_MASTER_KEY:-}" ]]; then
  export LITELLM_API_KEY="${LITELLM_MASTER_KEY}"
fi
export LITELLM_BASE_URL LITELLM_OPENAI_BASE
if [[ -n "${LITELLM_API_KEY:-}" ]]; then
  export LITELLM_API_KEY
  # OpenAI-compat agents
  export OPENAI_API_KEY="${LITELLM_API_KEY}"
  export OPENAI_BASE_URL="${LITELLM_OPENAI_BASE}"
  # Gemini ACP → LiteLLM
  export GOOGLE_GEMINI_BASE_URL="${LITELLM_BASE_URL}"
  export GEMINI_API_KEY="${LITELLM_API_KEY}"
  export GOOGLE_API_KEY="${LITELLM_API_KEY}"
fi

# Codex ChatGPT session → ACPX_AUTH_CHAT_GPT only (do not override OPENAI_API_KEY
# when LiteLLM is configured — that would steal coding-agent traffic).
if [[ -z "${ACPX_AUTH_CHAT_GPT:-}" && -f "$HOME/.codex/auth.json" ]]; then
  # shellcheck disable=SC1091
  eval "$(
    python3 - <<'PY'
import json, shlex
from pathlib import Path
p = Path.home() / ".codex" / "auth.json"
try:
    d = json.loads(p.read_text())
except Exception:
    raise SystemExit(0)
tok = (d.get("tokens") or {}).get("access_token")
if tok:
    print(f"export ACPX_AUTH_CHAT_GPT={shlex.quote(tok)}")
# Only set OPENAI_API_KEY from codex auth if LiteLLM is not active
import os
if not os.environ.get("LITELLM_API_KEY"):
    key = d.get("OPENAI_API_KEY")
    if key:
        print(f"export OPENAI_API_KEY={shlex.quote(key)}")
        print(f"export ACPX_AUTH_API_KEY={shlex.quote(key)}")
PY
  )" || true
fi

# Gemini API key file fallback only when LiteLLM not configured
if [[ -z "${LITELLM_API_KEY:-}" && -z "${GEMINI_API_KEY:-}${GOOGLE_API_KEY:-}" ]]; then
  for f in "$HOME/.config/gemini/api_key" "$HOME/.gemini/api_key"; do
    if [[ -f "$f" ]]; then
      export GEMINI_API_KEY
      GEMINI_API_KEY="$(tr -d '\n' <"$f")"
      export GOOGLE_API_KEY="${GOOGLE_API_KEY:-$GEMINI_API_KEY}"
      break
    fi
  done
fi

exec /home/chatgpt/.npm-global/bin/acpx "$@"
