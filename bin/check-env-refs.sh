#!/usr/bin/env bash
# check-env-refs.sh — verify servers.json ${VAR} refs resolve (names + lengths only).
# ${VAR:-default} refs carry a fallback, so they always resolve and are never
# reported missing.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

load_env_file() {
  local f="$1"
  [ -f "$f" ] || return 0
  set -a
  # shellcheck source=/dev/null
  source "$f"
  set +a
}

load_env_file "${HOME}/.config/swarm/.env"
load_env_file "${ROOT}/.env"

# Dev-box fallback: without the gitignored live manifest, check the example
# (refs will mostly not resolve here — that is expected and reported honestly).
MANIFEST="${ROOT}/servers.json"
if [ ! -f "$MANIFEST" ]; then
  MANIFEST="${ROOT}/servers.example.json"
  echo "(servers.json absent — checking refs in servers.example.json; NOT the live manifest)"
fi

export FLYIO_API_KEY="${FLYIO_API_KEY:-${FLY_API_KEY:-}}"
export VIRUSTOTAL_API_KEY="${VIRUSTOTAL_API_KEY:-${VT_API_KEY:-}}"
export WOLFRAMALPHA_API_KEY="${WOLFRAMALPHA_API_KEY:-${WOLFRAM_LLM_APP_ID:-}}"
export OPEN_SWARM_API_KEY="${OPEN_SWARM_API_KEY:-${API_AUTH_TOKEN:-${SWARM_API_KEY:-}}}"
export TERMINAL_API_KEY="${TERMINAL_API_KEY:-${API_TOKEN:-}}"
export GITHUB_API_KEY="${GITHUB_API_KEY:-${GITHUB_PERSONAL_ACCESS_TOKEN:-}}"

python3 - "$MANIFEST" <<'PY'
import os, re, sys
from pathlib import Path

# Optional: server is manual/disabled until creds exist
OPTIONAL = {"BOX_API_KEY"}

text = Path(sys.argv[1]).read_text()
refs = sorted(set(re.findall(r"\$\{([^}]+)\}", text)))
missing, ok, optional_empty, defaults = [], [], [], []
for r in refs:
    if ":-" in r:
        # ${VAR:-default}: a ref with a default is never "missing" — it resolves
        # from VAR when set and non-empty, from the default otherwise
        # (the split itself lives in bin/expand_env.split_ref).
        defaults.append(r)
        continue
    v = os.getenv(r) or ""
    if not v:
        (optional_empty if r in OPTIONAL else missing).append(r)
    else:
        ok.append((r, len(v)))

print(f"resolved {len(ok) + len(defaults)}/{len(refs)} required env refs from servers.json")
for r, n in ok:
    print(f"  OK  {r} (len={n})")
for r in defaults:
    print(f"  OK  {r} (has a default — resolves with or without the var)")
for r in optional_empty:
    print(f"  ~~  {r} empty (optional / manual-only server)")
if missing:
    print("MISSING (set in mcp-gateway/.env or ~/.config/swarm/.env):")
    for r in missing:
        print(f"  --  {r}")
    sys.exit(1)
print("all required ${VAR} refs resolve")
PY
