#!/bin/bash
# scale-replicas.sh — resource-guarded horizontal autoscaler for stateless
# mcp-openapi-proxy replicas (issue #34).
#
# Why: mcp-openapi-proxy runs a single uvicorn worker with a blocking event
# loop, so one bridge process is an availability/throughput ceiling under
# concurrent tool calls. The proxy is stateless (no Mcp-Session-Id / sticky
# routing), so we scale a pool of replicas and let nginx (least_conn) balance.
# Each replica is a managed systemd unit: mcp-replica@<port>.service.
#
# This script is the *safe* way to grow that pool. Growing blindly is how a
# 2-core host gets tipped over, so before adding a replica it enforces:
#
#   1. Hard cap            MAX_REPLICAS (absolute ceiling, default 4).
#   2. CPU headroom        1-min load average must stay under a per-core
#                          budget (LOAD_PER_CORE * nproc) with SCALE_MARGIN
#                          reserved, else refuse to add.
#   3. Memory headroom     estimated replica RSS * headroom must fit in
#                          MemAvailable, with MEM_MARGIN_MB kept free.
#   4. Disk headroom       refuse if root disk free < DISK_MIN_MB.
#   5. Exponential backoff ADD_BACKOFF_BASE * 2^n seconds since the last
#      add, so a thrashing/upstream-broken service doesn't spawn units in a
#      tight loop; each add also waits ADD_COOLDOWN_SEC between steps.
#   6. Removal is always allowed (to relieve pressure) and is immediate, but
#      never drops below MIN_REPLICAS.
#
# nginx is NOT reconfigured here — it fronts a fixed port range in the
# `gpt_terminal_plus` upstream with least_conn + max_fails, and marks a replica
# down (fail_timeout) automatically if it is stopped or unhealthy. This script
# only starts/stops the systemd replica units; nginx does the balancing.
#
# Usage:
#   scale-replicas.sh status    # print current replicas + resource snapshot
#   scale-replicas.sh up        # add one replica if all guards pass
#   scale-replicas.sh down      # remove one replica (never below MIN)
#   scale-replicas.sh sync      # ensure enabled units match desired count
#   scale-replicas.sh timer     # one-shot check suitable for a systemd timer
set -uo pipefail

# ---------------------------------------------------------------- tunables ---
MAX_REPLICAS="${MAX_REPLICAS:-4}"
MIN_REPLICAS="${MIN_REPLICAS:-1}"
PORT_BASE="${PORT_BASE:-8915}"
PORT_STEP=1

# CPU: allow the 1-min load to reach this multiple of nproc minus a reserved
# margin, before we consider the box too busy to add work.
LOAD_PER_CORE="${LOAD_PER_CORE:-1.0}"
SCALE_MARGIN="${SCALE_MARGIN:-0.5}"        # keep this many cores' worth of slack
# Memory: rough per-replica RSS (wrapper+uv+proxy), plus a safety multiplier.
REPLICA_RSS_MB="${REPLICA_RSS_MB:-120}"
MEM_HEADROOM_MULT="${MEM_HEADROOM_MULT:-2}" # reserve ~2 replicas worth of RAM
MEM_MARGIN_MB="${MEM_MARGIN_MB:-1500}"      # always keep this much free
DISK_MIN_MB="${DISK_MIN_MB:-1500}"

# Backoff: delay since-last-add = BASE * 2^(adds since last steady state).
ADD_BACKOFF_BASE="${ADD_BACKOFF_BASE:-30}"
ADD_COOLDOWN_SEC="${ADD_COOLDOWN_SEC:-20}"

STATE_DIR="${MCP_GATEWAY_STATE:-$HOME/.state/mcp-gateway}"
SCALE_STATE="$STATE_DIR/scale-replicas.state"
UNIT_PREFIX="mcp-replica@"

mkdir -p "$STATE_DIR"

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

# ------------------------------------------------------------ host metrics ---
ncpu() { nproc 2>/dev/null || echo 1; }
load1() { awk '{print $1}' /proc/loadavg 2>/dev/null || echo 0; }
mem_avail_mb() { free -m | awk '/^Mem:/{print $7}'; }
disk_free_mb() { df -Pm / | awk 'NR==2{print $4}'; }

# ------------------------------------------------------------ replica mgmt ---
port_for_index() { echo $(( PORT_BASE + $1 )); }         # index -> port
unit_for_port() { echo "${UNIT_PREFIX}$1.service"; }
desired_count() { cat "$SCALE_STATE" 2>/dev/null | head -1 || echo "$MIN_REPLICAS"; }
running_indices() {
  # echo indices [start..] whose unit is active AND port is listening
  local i p u
  for i in $(seq 0 $(( MAX_REPLICAS - 1 ))); do
    p=$(port_for_index "$i"); u=$(unit_for_port "$p")
    if systemctl --user is-active "$u" >/dev/null 2>&1 && ss -tlnH 2>/dev/null | grep -q ":$p "; then
      echo "$i"
    fi
  done
}
count_running() { running_indices | wc -l | tr -d ' '; }

now() { date +%s; }
state_get() { # key
  grep -E "^$1=" "$SCALE_STATE" 2>/dev/null | tail -1 | cut -d= -f2-
}
state_set() { # key val
  grep -vE "^$1=" "$SCALE_STATE" 2>/dev/null > "$SCALE_STATE.tmp" || true
  echo "$1=$2" >> "$SCALE_STATE.tmp"
  mv "$SCALE_STATE.tmp" "$SCALE_STATE"
}

# last_add_ts / add_streak drive the exponential backoff.
can_add_now() {
  local last streak elapsed backoff
  last=$(state_get last_add_ts); last=${last:-0}
  streak=$(state_get add_streak); streak=${streak:-0}
  elapsed=$(( $(now) - last ))
  # backoff doubles per consecutive add: 30,60,120,240,... capped at 15m
  backoff=$(( ADD_BACKOFF_BASE * (1 << (streak < 6 ? streak : 6)) ))
  [ "$backoff" -gt 900 ] && backoff=900
  if [ "$elapsed" -lt "$backoff" ]; then
    log "ADD blocked by backoff: ${elapsed}s since last add < ${backoff}s (streak=$streak)"
    return 1
  fi
  return 0
}

resource_ok_for_add() {
  local cores load budget avail need
  cores=$(ncpu); load=$(load1)
  # budget = cores*LOAD_PER_CORE - SCALE_MARGIN  (min 0.25 core)
  budget=$(awk -v c="$cores" -v lpc="$LOAD_PER_CORE" -v m="$SCALE_MARGIN" \
    'BEGIN{v=c*lpc-m; if(v<0.25)v=0.25; printf "%.2f", v}')
  if awk -v l="$load" -v b="$budget" 'BEGIN{exit !(l>=b)}'; then
    log "ADD blocked by CPU: load1=${load} >= budget=${budget} (cores=${cores})"
    return 1
  fi

  avail=$(mem_avail_mb)
  need=$(( REPLICA_RSS_MB * MEM_HEADROOM_MULT ))
  if [ "$(( avail - need ))" -lt "$MEM_MARGIN_MB" ]; then
    log "ADD blocked by MEM: avail=${avail}MB, need=${need}MB, margin=${MEM_MARGIN_MB}MB"
    return 1
  fi

  local dfree; dfree=$(disk_free_mb)
  if [ "${dfree:-0}" -lt "$DISK_MIN_MB" ]; then
    log "ADD blocked by DISK: free=${dfree}MB < ${DISK_MIN_MB}MB"
    return 1
  fi
  return 0
}

add_one() {
  local have idx port unit
  have=$(count_running)
  if [ "$have" -ge "$MAX_REPLICAS" ]; then
    log "ADD skipped: at cap ($have/$MAX_REPLICAS)"
    return 0
  fi
  can_add_now || return 0
  resource_ok_for_add || return 0

  # start the first free index
  idx=""
  for i in $(seq 0 $(( MAX_REPLICAS - 1 ))); do
    port=$(port_for_index "$i"); unit=$(unit_for_port "$port")
    if ! systemctl --user is-active "$unit" >/dev/null 2>&1; then idx="$i"; break; fi
  done
  [ -z "$idx" ] && { log "ADD skipped: no free index"; return 0; }

  port=$(port_for_index "$idx"); unit=$(unit_for_port "$port")
  systemctl --user enable --now "$unit" >/dev/null 2>&1
  local streak; streak=$(state_get add_streak); streak=${streak:-0}
  state_set last_add_ts "$(now)"
  state_set add_streak $(( streak + 1 ))
  state_set desired $(( have + 1 ))
  log "ADDED replica :$port (now $(( have + 1 ))/$MAX_REPLICAS, add_streak=$(( streak + 1 )))"
}

remove_one() {
  local running idx last port unit
  running=$(running_indices)
  local have; have=$(echo "$running" | grep -c . )
  if [ "$have" -le "$MIN_REPLICAS" ]; then
    log "REMOVE skipped: at floor ($have/$MIN_REPLICAS)"
    return 0
  fi
  # remove the highest-index running replica
  idx=$(echo "$running" | tail -1)
  port=$(port_for_index "$idx"); unit=$(unit_for_port "$port")
  systemctl --user disable --now "$unit" >/dev/null 2>&1
  state_set add_streak 0
  state_set desired $(( have - 1 ))
  log "REMOVED replica :$port (now $(( have - 1 ))/$MAX_REPLICAS))"
}

status() {
  local cores load avail dfree have
  cores=$(ncpu); load=$(load1); avail=$(mem_avail_mb); dfree=$(disk_free_mb); have=$(count_running)
  echo "replicas: $have/$MAX_REPLICAS (min $MIN_REPLICAS)"
  echo "ports:    $(running_indices | while read i; do printf ':%s ' "$(port_for_index "$i")"; done)"
  echo "cpu:      cores=$cores load1=$load budget=$(awk -v c=$cores -v lpc=$LOAD_PER_CORE -v m=$SCALE_MARGIN 'BEGIN{v=c*lpc-m;if(v<0.25)v=0.25;printf "%.2f",v}')"
  echo "mem:      avail=${avail}MB (need ~$(( REPLICA_RSS_MB * MEM_HEADROOM_MULT ))MB, margin ${MEM_MARGIN_MB}MB)"
  echo "disk:     free=${dfree}MB (min ${DISK_MIN_MB}MB)"
  echo "backoff:  streak=$(state_get add_streak) last_add=$(state_get last_add_ts)"
}

# One-shot "timer" action: grow only if we're healthy and below target; never
# shrinks (relief is an explicit `down` or an external pressure alarm).
timer_tick() {
  local have; have=$(count_running)
  if [ "$have" -lt "$MIN_REPLICAS" ]; then
    add_one
  elif [ "$have" -lt "$MAX_REPLICAS" ]; then
    # Only pre-warm to MAX when the host is comfortably idle; guards still apply.
    resource_ok_for_add && add_one || true
  fi
}

case "${1:-status}" in
  up)     add_one ;;
  down)   remove_one ;;
  status) status ;;
  sync)   # ensure desired units are running/enabled
          while [ "$(count_running)" -lt "$(desired_count)" ]; do add_one; sleep "$ADD_COOLDOWN_SEC"; done ;;
  timer)  timer_tick ;;
  *)      status; echo "usage: $0 {status|up|down|sync|timer}" ;;
esac