#!/bin/bash
# dashboard.sh — snapshot fallback for the live status WebUI (mcp-status :8781).
# Watchdog still writes status.html in case the live service is down.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/status.html"
exec python3 "$ROOT/bin/status_collect.py" --html "$OUT"
