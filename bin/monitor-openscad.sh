#!/bin/bash
# Monitor OpenSCAD MCP stack for 2h; analyze + auto-fix every 5 minutes.
set -uo pipefail

DURATION_SEC="${1:-7200}"
INTERVAL_SEC=300
LOG="${MCP_GATEWAY_STATE:-$HOME/.state/mcp-gateway}/openscad-monitor.log"
SUMMARY="${MCP_GATEWAY_STATE:-$HOME/.state/mcp-gateway}/openscad-monitor-summary.json"

log() { echo "[$(date -Iseconds)] $*" | tee -a "$LOG"; }

fix_port_conflict() {
  local port="$1" keep_pattern="$2"
  mapfile -t pids < <(ss -tlnp 2>/dev/null | grep ":${port}\\b" | grep -oP 'pid=\\K[0-9]+' | sort -u)
  for pid in "${pids[@]:-}"; do
    [ -z "$pid" ] && continue
    if ! tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -qE "$keep_pattern"; then
      log "FIX kill pid=$pid on :$port"
      kill "$pid" 2>/dev/null || sudo kill "$pid" 2>/dev/null || true
    fi
  done
}

auto_fix() {
  sudo systemctl disable --now mcp-server@openscad 2>/dev/null || true
  fix_port_conflict 8816 'launch-cli-server|mcp_openapi_proxy_cli|mcp-cli-server'
  fix_port_conflict 3860 'cli_bridge.py serve --config .*/configs/openscad.json'
  if ! systemctl is-active --quiet mcp-cli-server@openscad; then
    log "FIX restart mcp-cli-server@openscad"
    sudo systemctl restart mcp-cli-server@openscad 2>/dev/null || systemctl restart mcp-cli-server@openscad || true
    sleep 5
  fi
}

analyze_check() {
  python3 - <<'PY'
import json, os, subprocess, urllib.request, urllib.error

log_line = lambda msg: print(msg, flush=True)
issues = []
fixes = []

def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)

# systemd
st = sh("systemctl is-active mcp-cli-server@openscad").stdout.strip()
legacy = sh("systemctl is-active mcp-server@openscad 2>/dev/null").stdout.strip()
log_line(f"systemd mcp-cli-server@openscad={st} legacy={legacy}")
if st != "active":
    issues.append("cli-server inactive")
if legacy == "active":
    issues.append("legacy mcp-server@openscad conflict")

# ports
for port in (3860, 8816):
    r = sh(f"ss -tlnp | grep ':{port} '")
    if r.returncode != 0:
        issues.append(f"port {port} not listening")

# grok-style MCP flow
MCP = "http://127.0.0.1:8816/mcp"
h = {"Accept": "application/json, text/event-stream", "User-Agent": "grok-connectors-manager/0.1.0"}
tools_bytes = 0
tools = []
render_ok = False
try:
    req = urllib.request.Request(MCP, data=json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "grok-connectors-manager", "version": "0.1.0"}},
    }).encode(), headers={**h, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        sid = resp.headers.get("mcp-session-id")
        init_bytes = len(resp.read())
    hh = {**h, "Content-Type": "application/json", "mcp-session-id": sid}
    urllib.request.urlopen(urllib.request.Request(
        MCP, data=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}).encode(),
        headers=hh, method="POST"), timeout=30).read()
    req = urllib.request.Request(MCP, data=json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
    }).encode(), headers=hh, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode()
    tools_bytes = len(body)
    for ln in body.splitlines():
        if ln.strip().startswith("data:"):
            ev = json.loads(ln.strip()[5:])
            if ev.get("id") == 2:
                tools = [t["name"] for t in ev.get("result", {}).get("tools", [])]
    log_line(f"MCP init={init_bytes}B tools/list={tools_bytes}B tools={tools}")
    if tools_bytes < 1000:
        issues.append(f"tools/list truncated ({tools_bytes}B) — grok will fail")
    if "render_png" not in tools or "render_stl" not in tools:
        issues.append(f"missing render tools: {tools}")
    # tools/call smoke
    req = urllib.request.Request(MCP, data=json.dumps({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "render_png", "arguments": {
            "scad": "cube(3);", "imgsize": "32,32", "render": True,
            "autocenter": True, "viewall": True,
        }},
    }).encode(), headers=hh, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = resp.read().decode()
    for ln in body.splitlines():
        if ln.strip().startswith("data:"):
            ev = json.loads(ln.strip()[5:])
            if ev.get("id") == 3:
                types = [c.get("type") for c in ev.get("result", {}).get("content", [])]
                render_ok = "image" in types
                log_line(f"render_png types={types}")
    if not render_ok:
        issues.append("render_png missing ImageContent")
except Exception as e:
    issues.append(f"MCP flow error: {e}")
    log_line(f"MCP ERROR {e}")

# nginx grok traffic (last 5 min)
nginx = sh("sudo grep openscad /var/log/nginx/glama-bridge.access.log 2>/dev/null | tail -15")
if nginx.stdout:
    lines = [l for l in nginx.stdout.strip().splitlines() if l]
    statuses = [l.split('"')[2].strip().split()[0] for l in lines if 'HTTP/1.1' in l]
    has_call = any("POST" in l and " 200 " in l and len(l.split()[-1]) > 200 for l in lines)
    log_line(f"nginx last {len(lines)} openscad hits statuses={statuses[-6:]}")
    if any(s.startswith('4') or s.startswith('5') for s in statuses):
        issues.append(f"nginx errors: {statuses}")
else:
    log_line("nginx no recent openscad traffic")

# openscad-specific servers.log errors only (ignore other ports' EADDRINUSE)
sl = sh("tail -50 /home/chatgpt/.state/mcp-gateway/servers.log 2>/dev/null | grep -iE 'openscad|:8816|:3860|cli-bridge.*3860' | grep -iE 'EADDRINUSE|FAIL|error|Connection refused' || true")
if sl.stdout.strip():
    issues.append("openscad servers.log errors")
    log_line(f"servers.log: {sl.stdout.strip()[:200]}")

status = "OK" if not issues else "ISSUES"
log_line(f"RESULT {status}: {issues or ['none']}")
print(json.dumps({"status": status, "issues": issues, "tools_bytes": tools_bytes, "tools": tools, "render_ok": render_ok}))
PY
}

write_summary() {
  local iter="$1" ok="$2" fail="$3"
  python3 - <<PY
import json, datetime
p = "$SUMMARY"
try:
    data = json.load(open(p)) if __import__('os').path.exists(p) else {"checks": []}
except Exception:
    data = {"checks": []}
data["checks"].append({"iter": $iter, "time": datetime.datetime.now().isoformat(), "ok": $ok, "fail": $fail})
data["last_update"] = datetime.datetime.now().isoformat()
json.dump(data, open(p, "w"), indent=2)
PY
}

log "=== AFK monitor start: ${DURATION_SEC}s interval=${INTERVAL_SEC}s (sleep 300) ==="
end=$((SECONDS + DURATION_SEC))
iter=0
ok_total=0
fail_total=0

while [ "$SECONDS" -lt "$end" ]; do
  iter=$((iter + 1))
  log "--- check #${iter} ($(date)) ---"
  result=$(analyze_check 2>&1 | tee -a "$LOG") || true
  if echo "$result" | grep -qE 'RESULT OK|"status": "OK"'; then
    ok_total=$((ok_total + 1))
    log "ANALYSIS OK"
  else
    fail_total=$((fail_total + 1))
    log "ANALYSIS ISSUES — running auto_fix"
    auto_fix | tee -a "$LOG" || true
    sleep 5
    analyze_check 2>&1 | tee -a "$LOG" || true
  fi
  write_summary "$iter" "$ok_total" "$fail_total"
  remaining=$((end - SECONDS))
  [ "$remaining" -le 0 ] && break
  log "sleep 300 (${remaining}s remaining)"
  sleep 300
done

log "=== monitor complete: ${iter} checks, ok=${ok_total} fail=${fail_total} ==="