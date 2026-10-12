#!/usr/bin/env bash
# disk-guard.sh — last-resort free space for gateway LOG WRITES only.
#
# Architecture:
#   • logrotate (/etc/logrotate.d/mcp-gateway) owns rotation + deletion of old
#     generations. This script does NOT rotate and does NOT rm rotated logs.
#   • safe-log-run.py owns "never die on ENOSPC" while a process is running.
#   • This script only, on CRIT disk: truncate active STATE/*.log in place so
#     the next service write can succeed. No rm of anything else. Ever.
#
# Why so narrow: a prior version tried DIY "keep N rotated" reclaim, listed CWD
# when globs were empty, and rm -f'd relative names — deleting home/project files.
#
# Exit: 0 ok, 1 warn (disk low, no mutate), 2 crit (truncated active logs), 3 safety abort, 4 self-test fail
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
QUIET=0
SELFTEST=0
for arg in "$@"; do
  case "$arg" in
    --quiet) QUIET=1 ;;
    --self-test) SELFTEST=1 ;;
    -h|--help)
      echo "usage: disk-guard.sh [--quiet] [--self-test]"
      echo "  CRIT only: truncate active STATE/*.log (no rm, no rotation)."
      echo "  Rotation/deletion: logrotate only."
      exit 0
      ;;
  esac
done

# Warn = note only. Crit = truncate active *.log under STATE. No other thresholds delete.
WARN_MB="${MCP_DISK_WARN_MB:-1024}"
CRIT_MB="${MCP_DISK_CRIT_MB:-300}"

die_safety() {
  printf '%s disk-guard ERROR safety: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2 || true
  exit 3
}

abs_normalize() {
  local p="${1-}"
  [ -n "$p" ] || { echo ""; return 1; }
  case "$p" in
    ""|"."|".."|"/"|"//") echo ""; return 1 ;;
    /*) ;;
    *) echo ""; return 1 ;;
  esac
  python3 -c 'import os,sys; print(os.path.normpath(sys.argv[1]))' "$p" 2>/dev/null || { echo ""; return 1; }
}

path_depth() {
  python3 -c 'import sys; p=sys.argv[1].strip("/"); print(0 if not p else len([x for x in p.split("/") if x]))' "$1"
}

validate_state_dir() {
  local raw="${1-}" home_n state_n base depth real

  [ -n "$raw" ] || die_safety "STATE is empty"
  case "$raw" in
    ""|" "|"."|".."|"/"|"//"|~|"~/") die_safety "STATE is forbidden literal: ${raw@Q}" ;;
  esac

  state_n="$(abs_normalize "$raw")" || die_safety "STATE not absolute: ${raw@Q}"
  [ -n "$state_n" ] || die_safety "STATE normalized empty"
  [ "$state_n" != "/" ] || die_safety "STATE is /"

  [ -n "${HOME-}" ] || die_safety "HOME is empty"
  home_n="$(abs_normalize "$HOME")" || die_safety "HOME not normalizable"
  [ -n "$home_n" ] && [ "$home_n" != "/" ] || die_safety "HOME unsafe"
  [ "$state_n" != "$home_n" ] || die_safety "STATE equals HOME"

  depth="$(path_depth "$state_n")"
  [ "$depth" -ge 4 ] || die_safety "STATE too shallow (depth=$depth): $state_n"
  base="$(basename "$state_n")"
  [ "$base" = "mcp-gateway" ] || die_safety "STATE basename must be mcp-gateway (got $base)"
  if [ "$(basename "$(dirname "$state_n")")" != ".state" ] && [ -z "${MCP_GATEWAY_STATE:-}" ]; then
    die_safety "STATE parent must be .state unless MCP_GATEWAY_STATE is set intentionally"
  fi

  if [ -e "$state_n" ] || [ -L "$state_n" ]; then
    [ -d "$state_n" ] || die_safety "STATE is not a directory: $state_n"
    real="$(readlink -f "$state_n" 2>/dev/null || true)"
    [ -n "$real" ] || die_safety "readlink -f failed"
    [ "$real" != "/" ] && [ "$real" != "$home_n" ] || die_safety "STATE realpath unsafe: $real"
    [ "$(basename "$real")" = "mcp-gateway" ] || die_safety "STATE realpath basename not mcp-gateway"
    [ "$(path_depth "$real")" -ge 4 ] || die_safety "STATE realpath too shallow"
    state_n="$real"
  fi
  printf '%s' "$state_n"
}

# Only allow truncate of regular files: STATE_REAL/<name>.log
assert_active_log() {
  local f="${1-}" real base
  [ -n "$f" ] || return 1
  case "$f" in
    /*) ;;
    *) return 1 ;;
  esac
  [ -f "$f" ] || return 1
  [ ! -d "$f" ] || return 1
  real="$(readlink -f "$f" 2>/dev/null || true)"
  [ -n "$real" ] || return 1
  case "$real" in
    "$STATE_REAL"/*) ;;
    *) return 1 ;;
  esac
  [ "$real" != "$STATE_REAL" ] && [ "$real" != "/" ] && [ "$real" != "$HOME_REAL" ] || return 1
  base="$(basename "$real")"
  case "$base" in
    *.log) ;;
    *) return 1 ;;
  esac
  # refuse names that look like rotated generations (logrotate's job)
  case "$base" in
    *.log-*|*.log.gz|*.log.*.gz) return 1 ;;
  esac
  return 0
}

_log() {
  local line
  line="$(date -u '+%Y-%m-%dT%H:%M:%SZ') disk-guard $*"
  if ! (printf '%s\n' "$line" >>"$LOG") 2>/dev/null; then
    printf '%s\n' "$line" >&2 || true
  fi
}

avail_mb() {
  python3 - "$STATE_REAL" <<'PY'
import os, sys
path = sys.argv[1]
while path and not os.path.isdir(path):
    parent = os.path.dirname(path)
    if parent == path:
        path = "/"
        break
    path = parent
st = os.statvfs(path or "/")
print(int(st.f_bavail * st.f_frsize / (1024 * 1024)))
PY
}

# CRIT only: empty active *.log so writers can continue. No rm. No rotation.
crit_truncate_active_logs() {
  local f
  _log "CRIT truncate active *.log under $STATE_REAL only (no rm; logrotate owns rotation)"
  shopt -s nullglob
  for f in "$STATE_REAL"/*.log; do
    assert_active_log "$f" || continue
    printf '%s disk-guard: truncated active log (CRIT disk; rotation is logrotate)\n' \
      "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" >"$f" 2>/dev/null || true
    _log "CRIT truncated $(basename "$f")"
  done
  shopt -u nullglob
}

run_self_test() {
  local script="$ROOT/bin/disk-guard.sh" rc fail=0
  refuse() {
    local label="$1"; shift
    rc=0
    env "$@" "$script" --quiet >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 3 ]; then echo "PASS refuse: $label"
    else echo "FAIL refuse: $label (exit $rc want 3)"; fail=1
    fi
  }
  refuse "STATE=/" MCP_GATEWAY_STATE=/
  rc=0; env MCP_GATEWAY_STATE= "$script" --quiet >/dev/null 2>&1 || rc=$?
  [ "$rc" -eq 3 ] && echo "PASS refuse: STATE=empty" || { echo "FAIL refuse: STATE=empty ($rc)"; fail=1; }
  refuse "STATE=HOME" MCP_GATEWAY_STATE="$HOME"
  refuse "STATE=." MCP_GATEWAY_STATE="."
  refuse "STATE=relative" MCP_GATEWAY_STATE="mcp-gateway"
  refuse "STATE=/tmp" MCP_GATEWAY_STATE=/tmp
  rc=0; env -u MCP_GATEWAY_STATE "$script" --quiet >/dev/null 2>&1 || rc=$?
  if [ "$rc" -eq 3 ] || [ "$rc" -eq 4 ]; then
    echo "FAIL default STATE should be allowed ($rc)"; fail=1
  else
    echo "PASS default STATE allowed (exit $rc)"
  fi
  # Ensure script source has no rm of rotated patterns in main path
  if grep -E 'rm[[:space:]]+(-[a-zA-Z]*f|-[a-zA-Z]*r)' "$script" | grep -v '^#' | grep -qv self-test; then
    # allow no rm at all in production path — this script should not rm
    if grep -nE '^\s*rm\s' "$script" | grep -v self-test | grep -v '^[[:space:]]*#'; then
      echo "WARN: rm still present in disk-guard (review)"
    fi
  fi
  if ! grep -q 'logrotate' "$script"; then
    echo "FAIL docs should mention logrotate owns rotation"; fail=1
  else
    echo "PASS documents logrotate ownership"
  fi
  [ "$fail" -eq 0 ] || exit 4
  echo "self-test OK"
  exit 0
}

[ "$SELFTEST" = 1 ] && run_self_test

if [ "${MCP_GATEWAY_STATE+x}" = x ]; then
  STATE_IN="${MCP_GATEWAY_STATE}"
else
  STATE_IN="${HOME}/.state/mcp-gateway"
fi
STATE_REAL="$(validate_state_dir "$STATE_IN")"
HOME_REAL="$(abs_normalize "$HOME")"
LOG="${STATE_REAL}/watchdog.log"
mkdir -p "${STATE_REAL}" "${STATE_REAL}/locks" 2>/dev/null || true
STATE_REAL="$(validate_state_dir "$STATE_REAL")"
LOG="${STATE_REAL}/watchdog.log"

mb="$(avail_mb 2>/dev/null || echo 99999)"
case "$mb" in ''|*[!0-9]*) mb=99999 ;; esac

if [ "$mb" -le "$CRIT_MB" ]; then
  _log "CRIT avail=${mb}MB (<=${CRIT_MB}MB) state=$STATE_REAL"
  crit_truncate_active_logs
  _log "CRIT after truncate avail=$(avail_mb 2>/dev/null || echo '?')MB"
  exit 2
fi

if [ "$mb" -le "$WARN_MB" ]; then
  # Warn only — do not DIY rotate/delete; logrotate + operators handle this.
  _log "WARN avail=${mb}MB (<=${WARN_MB}MB) state=$STATE_REAL — no mutate (use logrotate)"
  exit 1
fi

[ "$QUIET" = 1 ] || _log "ok avail=${mb}MB state=$STATE_REAL"
exit 0
