#!/bin/bash
# Run the mcp-gateway test suite.
#
# Self-provisions a pytest venv on first run (uv), so it works on the live
# host and dev boxes alike — no reliance on a system pytest. Live-host tests
# (test_live_inventory.py, test_xdg_swarm_config_audit.py) auto-enable via a
# repo-root servers.json (or MCP_LIVE_HOST=1); see tests/live_host.py.
#
#   ./bin/run-tests.sh            # full suite
#   ./bin/run-tests.sh -k hygiene # pytest args pass through
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${MCP_TESTS_VENV:-$HOME/.local/venvs/mcp-tests}"
PY="$VENV/bin/python"
if [ ! -x "$PY" ]; then
  echo "[run-tests] creating test venv at $VENV" >&2
  uv venv "$VENV" >&2
  uv pip install --python "$PY" pytest >&2
fi
exec "$PY" -m pytest "$ROOT/tests" -q "$@"