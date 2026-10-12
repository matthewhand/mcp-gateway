#!/bin/bash
# watchdog.sh — health check + self-heal for the gateway. Run by a systemd timer.
# Restarts a bridge whose port is down (covers hung 'active' states beyond Restart=always),
# checks core services, and logs actions. Skips box (manual-only). Non-destructive.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Prefer root-disk state (~/.state), not ~/.local (often symlinked to /mnt/models).
STATE="${MCP_GATEWAY_STATE:-$HOME/.state/mcp-gateway}"
LOG="$STATE/watchdog.log"
LOCK="$STATE/locks/watchdog.lock"
COOLDOWN_DIR="$STATE/watchdog-cooldown"
mkdir -p "$(dirname "$LOG")" "$STATE/locks" "$COOLDOWN_DIR"
ts(){ date -u '+%Y-%m-%dT%H:%M:%SZ'; }
# Never die on log I/O (ENOSPC is exactly the failure mode we self-heal from).
log(){
  local line
  line="$(ts) $*"
  if ! (printf '%s\n' "$line" >>"$LOG") 2>/dev/null; then
    : >"$LOG" 2>/dev/null || true
    printf '%s\n' "$line" >>"$LOG" 2>/dev/null || printf '%s\n' "$line" >&2 || true
  fi
}

# Single-flight: system + user timers both exist historically; only one may act.
exec 9>"$LOCK"
if ! flock -n 9; then
  log "skip: another watchdog holds lock"
  exit 0
fi

# Disk first: reclaim log space before we try to restart anything that needs to write.
if [ -x "$ROOT/bin/disk-guard.sh" ]; then
  dg_rc=0
  "$ROOT/bin/disk-guard.sh" --quiet || dg_rc=$?
  if [ "$dg_rc" -ge 2 ]; then
    log "DISK crit reclaim ran (disk-guard exit $dg_rc)"
  elif [ "$dg_rc" -eq 1 ]; then
    log "DISK warn soft-reclaim ran (disk-guard exit $dg_rc)"
  fi
fi

# Per-unit cooldown (seconds) after we restart it
COOLDOWN_SEC="${MCP_WATCHDOG_COOLDOWN_SEC:-120}"
# Extra wait when MCP port is down but unit is already starting (CLI cold start)
CLI_GRACE_SEC="${MCP_WATCHDOG_CLI_GRACE_SEC:-45}"

unit_state() {
  systemctl --user show "$1" -p ActiveState -p SubState --value 2>/dev/null | tr '\n' ' '
}

in_cooldown() {
  local name="$1" f="$COOLDOWN_DIR/${name}.ts" now
  [ -f "$f" ] || return 1
  now=$(date +%s)
  local then
  then=$(cat "$f" 2>/dev/null || echo 0)
  [ $((now - then)) -lt "$COOLDOWN_SEC" ]
}

mark_cooldown() {
  date +%s > "$COOLDOWN_DIR/${1}.ts"
}

acted=0
# core: user units (mcp-*) + system nginx
if [ "$(systemctl --user is-active mcp-oauth 2>/dev/null)" != active ]; then
  st=$(unit_state mcp-oauth)
  if [[ "$st" != *activating* && "$st" != *auto-restart* ]]; then
    log "CORE mcp-oauth not active ($st) -> restart"
    systemctl --user restart mcp-oauth
    acted=1
  else
    log "CORE mcp-oauth $st — skip restart"
  fi
fi
if [ "$(systemctl --user is-active mcp-glama 2>/dev/null)" != active ]; then
  st=$(unit_state mcp-glama)
  if [[ "$st" != *activating* && "$st" != *auto-restart* ]]; then
    log "CORE mcp-glama not active ($st) -> restart"
    systemctl --user restart mcp-glama
    acted=1
  else
    log "CORE mcp-glama $st — skip restart"
  fi
fi
if [ "$(systemctl is-active nginx 2>/dev/null)" != active ]; then
  log "CORE nginx not active -> restart"
  sudo systemctl restart nginx
  acted=1
fi
if [ "$(systemctl --user is-active mcp-status 2>/dev/null)" != active ]; then
  st=$(unit_state mcp-status)
  if [[ "$st" != *activating* && "$st" != *auto-restart* ]]; then
    log "CORE mcp-status not active ($st) -> restart"
    systemctl --user restart mcp-status
    acted=1
  else
    log "CORE mcp-status $st — skip restart"
  fi
fi

# per-server bridges (from manifest; skip box/manual)
# CLI entries use mcp-cli-server@; pure OpenAPI use mcp-server@
mapfile -t ROWS < <(python3 -c "
import json
d=json.load(open('$ROOT/servers.json'))
print('glama 8765 mcp-glama 0')
for n,c in d.items():
    if n=='box' or str(c.get('enabled', True)).lower() == 'false':
        continue
    unit = 'mcp-cli-server@'+n if (c.get('cli_config') or c.get('cli_port')) else 'mcp-server@'+n
    cli = 1 if (c.get('cli_config') or c.get('cli_port')) else 0
    print(n, c['port'], unit, cli)
")
for row in "${ROWS[@]}"; do
  # name port unit is_cli
  name=$(echo "$row" | awk '{print $1}')
  port=$(echo "$row" | awk '{print $2}')
  unit=$(echo "$row" | awk '{print $3}')
  is_cli=$(echo "$row" | awk '{print $4}')

  if ss -tlnH | grep -q ":$port "; then
    continue
  fi

  # Port down — check unit state before thrashing
  st=$(unit_state "$unit")
  if [[ "$st" == *activating* || "$st" == *auto-restart* || "$st" == *reloading* ]]; then
    log "BRIDGE $name (:$port) down but unit $st — wait"
    # Give CLI cold-start time for MCP bind after openapi discovery
    grace=$([ "$is_cli" = 1 ] && echo "$CLI_GRACE_SEC" || echo 15)
    for _ in $(seq 1 "$grace"); do
      ss -tlnH | grep -q ":$port " && continue 2
      sleep 1
    done
    # still down?
    if ss -tlnH | grep -q ":$port "; then
      continue
    fi
    log "BRIDGE $name (:$port) still down after ${grace}s grace ($st) — leave Restart=always"
    continue
  fi

  if in_cooldown "$name"; then
    log "BRIDGE $name (:$port) down — cooldown active, skip"
    continue
  fi

  # Short re-check for pure OpenAPI; longer for CLI
  grace=$([ "$is_cli" = 1 ] && echo 15 || echo 5)
  sleep "$grace"
  if ss -tlnH | grep -q ":$port "; then
    continue
  fi

  # Prefer start if inactive/failed; restart only if "active" but port missing (hung)
  active=$(systemctl --user is-active "$unit" 2>/dev/null || true)
  if [ "$active" = active ]; then
    log "BRIDGE $name (:$port) down while unit active -> restart $unit"
    systemctl --user restart "$unit"
  else
    log "BRIDGE $name (:$port) down unit=$active -> start $unit"
    systemctl --user start "$unit"
  fi
  mark_cooldown "$name"
  acted=1
done

# MCP handshake self-heal: a bridge can be port-UP but wedged (event loop parked
# on a hung upstream), which the port check above cannot see — it logged "ok"
# while gpt-terminal-plus was unusable (issue #34). Probe the MCP handshake for
# enabled bridges and restart one that is listening but fails to initialize.
#
# Guards, so this can never thrash or self-deadlock:
#   * require N consecutive failures (default 2) before acting — one slow call
#     is not a wedge;
#   * honour the per-unit cooldown;
#   * gpt-terminal-plus is probed but NEVER auto-restarted here: it is the one
#     bridge a live client session depends on, and a spurious restart kills
#     in-flight interactive work (and an in-band probe from inside that session
#     can self-deadlock and look like a wedge). Its wedge is prevented at the
#     source by the outbound-request timeout (issue #34); if it ever does wedge
#     persistently, a human or the status page restarts it. Set
#     MCP_WATCHDOCK_RESTART_TERMINAL_PLUS=1 to opt in.
#   * skip units that are still activating/auto-restarting/deactivating.
HANDSHAKE_FAIL_THRESHOLD="${MCP_WATCHDOG_HANDSHAKE_FAILS:-2}"
HANDSHAKE_FAIL_DIR="$STATE/watchdog-handshake"
mkdir -p "$HANDSHAKE_FAIL_DIR"

if [ "${MCP_WATCHDOG_HANDSHAKE_PROBE:-1}" != "0" ]; then
  for row in "${ROWS[@]}"; do
    name=$(echo "$row" | awk '{print $1}')
    port=$(echo "$row" | awk '{print $2}')
    unit=$(echo "$row" | awk '{print $3}')
    [ "$name" = glama ] && continue

    # Only care about bridges that are actually listening (port-down is handled above).
    ss -tlnH | grep -q ":$port " || continue

    # Don't fight a unit that is mid-(re)start.
    st=$(unit_state "$unit")
    [[ "$st" == *activating* || "$st" == *auto-restart* || "$st" == *deactivating* ]] && continue

    # Warm-up grace: a just-started bridge fetches its OpenAPI spec from the
    # upstream on first request and legitimately cannot answer `initialize`
    # for a few seconds. Probing too early would false-positive and restart a
    # healthy bridge in a loop. Skip anything younger than the grace window.
    main_pid=$(systemctl --user show "$unit" -p MainPID --value 2>/dev/null || echo 0)
    if [ "${main_pid:-0}" -gt 0 ] 2>/dev/null && [ -r "/proc/$main_pid" ]; then
      started_ticks=$(awk '{print $22}' "/proc/$main_pid/stat" 2>/dev/null || echo "")
      if [ -n "$started_ticks" ]; then
        hz=$(getconf CLK_TCK 2>/dev/null || echo 100)
        up_secs=$(awk '{print int($1)}' /proc/uptime 2>/dev/null || echo 0)
        age=$(( up_secs - started_ticks / hz ))
        if [ "$age" -lt "${MCP_WATCHDOCK_WARMUP_SEC:-45}" ]; then
          continue
        fi
      fi
    fi

# Run the probe from an independent transient unit so a slow/hung bridge
    # cannot block the watchdog itself, and so the request is out-of-band.
    probe_ok=0
    if systemd-run --user --quiet --wait --pipe \
        /usr/bin/curl -sS --max-time "${MCP_WATCHDOG_HANDSHAKE_TIMEOUT:-6}" \
        -o /dev/null -X POST "http://127.0.0.1:$port/mcp" \
        -H 'Content-Type: application/json' -H 'Accept: application/json' \
        -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"mcp-watchdog","version":"1"}}}' \
        >/dev/null 2>&1; then
      probe_ok=1
    fi

    f="$HANDSHAKE_FAIL_DIR/$name.count"
    if [ "$probe_ok" = 1 ]; then
      [ -f "$f" ] && rm -f "$f"
      continue
    fi

    n=$(( $(cat "$f" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$f"
    if [ "$n" -lt "$HANDSHAKE_FAIL_THRESHOLD" ]; then
      log "BRIDGE $name (:$port) listening but MCP handshake failed (${n}/${HANDSHAKE_FAIL_THRESHOLD}) — will recheck next cycle"
      continue
    fi

    # gpt-terminal-plus: log, but do NOT auto-restart (see header comment).
    # It is the interactive bridge a live client depends on; a spurious restart
    # kills in-flight work. Its wedge is prevented at the source by the
    # outbound-request timeout. Opt in with MCP_WATCHDOCK_RESTART_TERMINAL_PLUS=1.
    if [ "$name" = gpt-terminal-plus ] && [ "${MCP_WATCHDOCK_RESTART_TERMINAL_PLUS:-0}" != "1" ]; then
      log "BRIDGE $name (:$port) listening but MCP handshake failed ${n}x -> LOG ONLY (auto-restart disabled for interactive bridge; see issue #34). Manual: systemctl --user restart mcp-server@gpt-terminal-plus"
      rm -f "$f"
      continue
    fi

    if in_cooldown "$name"; then
      log "BRIDGE $name (:$port) handshake failed but cooldown active — skip restart"
      continue
    fi

    log "BRIDGE $name (:$port) listening but MCP handshake failed ${n}x -> restart $unit (wedged, issue #34)"
    systemctl --user restart "$unit"
    mark_cooldown "$name"
    rm -f "$f"
    acted=1
  done
fi


# dependency containers: a bridge can be "up" (port bound) but degraded if its backend container is down
declare -A DEP=( [netbox]=netbox-docker-netbox-1 [getzep]=zep-ce-zep-1 )
for svc in "${!DEP[@]}"; do
  cname="${DEP[$svc]}"
  if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$cname"; then
    log "WARN $svc backend container '$cname' not running -> $svc bridge degraded (serving stale/0 tools)"
  fi
done

# memory pressure note (don't act — just record; owner decides)
avail=$(free -m | awk '/Mem/{print $7}')
[ "${avail:-9999}" -lt 400 ] && log "WARN low memory: ${avail}MB available"

# disk free note (statvfs bavail on /)
disk_mb=$(python3 -c 'import os;s=os.statvfs("/");print(int(s.f_bavail*s.f_frsize/1024/1024))' 2>/dev/null || echo '?')
[ "${disk_mb:-9999}" != '?' ] && [ "${disk_mb}" -lt 1024 ] && log "WARN low disk: ${disk_mb}MB available on /"

[ "$acted" = 0 ] && log "ok: all healthy ($(free -m|awk '/Mem/{print $7}')MB mem, ${disk_mb}MB disk free)"
"$ROOT/bin/dashboard.sh" >/dev/null 2>&1 || true
exit 0
