#!/bin/bash
# status.sh — full one-command health snapshot of the gateway + its dependencies.
# Subcommand: ./status.sh safe-probe <name>  — deadlock-safe /healthz (never
# foreground-curls the caller's own port; see docs/RUNBOOK.md).
ROOT="$(cd "$(dirname "$0")" && pwd)"
# Host-specific public URL comes from local config (gitignored); default is
# portable so a fresh clone still prints something sensible.
[ -f "$ROOT/.env.local" ] && { set -a; . "$ROOT/.env.local"; set +a; }
PUBLIC_URL="${MCP_PUBLIC_URL:-https://mcp.example.com}"
if [ "${1:-}" = "safe-probe" ]; then
  shift
  exec "$ROOT/bin/safe-probe" "$@"
fi
echo "=== mcp-gateway status · $(date -u '+%Y-%m-%d %H:%M UTC') ==="
echo "-- webui --"
echo "  $PUBLIC_URL/  (htpasswd; also /status)"
printf "  %-22s %s\n" "mcp-status" "$(systemctl --user is-active mcp-status 2>/dev/null)"
echo "-- core --"
for u in mcp-oauth mcp-glama nginx fail2ban mcp-watchdog.timer; do
  if [ "$u" = nginx ] || [ "$u" = fail2ban ]; then
    printf "  %-22s %s\n" "$u" "$(systemctl is-active "$u" 2>/dev/null)"
  else
    printf "  %-22s %s\n" "$u" "$(systemctl --user is-active "$u" 2>/dev/null)"
  fi
done
echo "-- servers (port · state · creds · whitelist) --"
python3 - "$ROOT/servers.json" "$ROOT/bin" <<'PY'
import json,sys,subprocess
sys.path.insert(0, sys.argv[2])
from box_key import box_key_status
from pathlib import Path
mf = Path(sys.argv[1])
d=json.load(open(mf))
up=subprocess.run(['ss','-tlnH'],capture_output=True,text=True).stdout.replace('\t',' ')
rows={'glama':8765}; rows.update({k:v['port'] for k,v in d.items()})
box_gate = box_key_status(root=mf.parent, manifest=d) if "box" in d else {}
for s,p in sorted(rows.items(),key=lambda x:x[1]):
    st='UP ' if f':{p} ' in up else 'down'
    e=d.get(s,{}).get('env',{})
    cred='key' if any('KEY' in k or 'TOKEN' in k or 'HEADERS' in k for k in e) else '-'
    wl='wl' if 'TOOL_WHITELIST' in e else '-'
    extra = ""
    if s == "box" and box_gate.get("unhealthy"):
        extra = "  UNHEALTHY " + (box_gate.get("reason") or "short-lived Box token; refresh BOX_API_KEY")
        if st.strip() != "UP":
            st = "down"
    print(f"  {s:13} :{p}  {st}  {cred:3} {wl}{extra}")
PY
echo "-- containers --"
docker ps --format '  {{.Names}}  {{.Status}}' 2>/dev/null | head -14
echo "-- TLS --"
exp=$(sudo openssl x509 -enddate -noout -in /etc/letsencrypt/live/mcp/cert.pem 2>/dev/null | cut -d= -f2)
echo "  cert 'mcp' expires: ${exp:-?}"
echo "-- resources --"
free -m | awk '/Mem/{printf "  RAM: %sMB free / %sMB avail\n",$4,$7} /Swap/{printf "  swap: %sMB used\n",$3}'
df -h / /mnt/models | awk '/sd[ab]/{printf "  %s %s used (%s free)\n",$6,$5,$4}'
echo "-- security & watchdog --"
echo "  fail2ban banned: $(sudo fail2ban-client status mcp-gateway-auth 2>/dev/null | grep -oE 'Currently banned:.[0-9]+' | grep -oE '[0-9]+')"
echo "  watchdog last:   $(tail -1 ${MCP_GATEWAY_STATE:-$HOME/.state/mcp-gateway}/watchdog.log 2>/dev/null)"
echo "-- cli unit owner (user vs system; look-only) --"
mf="$ROOT/servers.json"
[ -f "$mf" ] || mf="$ROOT/servers.example.json"
python3 "$ROOT/bin/cli_unit_owner.py" --manifest "$mf" | sed 's/^/  /'
