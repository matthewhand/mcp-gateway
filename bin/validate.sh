#!/bin/bash
# validate.sh — sanity-check servers.json before/after editing.
# Checks: valid JSON · unique ports · ports in range · spec_url present ·
# nginx route present per server · (optional) spec reachability with --spec ·
# look-only user-vs-system mcp-cli-server@ overlap warning (never start/stop).
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MANIFEST="$ROOT/servers.json"
NGINX="/etc/nginx/sites-enabled/mcp.conf"
CHECK_SPEC=0; [ "${1:-}" = "--spec" ] && CHECK_SPEC=1
fail=0

# Dev-box fallback: without the gitignored live manifest, validate the tracked
# example instead (and skip the live nginx test below).
if [ ! -f "$MANIFEST" ]; then
  MANIFEST="$ROOT/servers.example.json"
  echo "  ⚠ servers.json MISSING on this host — validating servers.example.json (NOT the live manifest)."
  echo "    If this is the gateway host, restore servers.json and re-run!"
fi

python3 - "$MANIFEST" "$NGINX" "$CHECK_SPEC" <<'PY'
import json,sys,subprocess,re
from pathlib import Path
mf,ngx,checkspec=sys.argv[1],sys.argv[2],sys.argv[3]=="1"
sys.path.insert(0, str(Path(mf).parent / "bin"))
from secret_hygiene import check_manifest_secrets
from expand_env import expand
try: d=json.load(open(mf))
except Exception as e: print(f"  ✗ servers.json invalid JSON: {e}"); sys.exit(1)
ng=""
try: ng=open(ngx).read()
except Exception:
    try: ng=subprocess.run(["sudo","cat",ngx],capture_output=True,text=True).stdout
    except Exception: pass
ports={}; bad=0
for name,c in d.items():
    p=c.get("port")
    if not p: print(f"  ✗ {name}: no port"); bad+=1; continue
    if not (8000<=p<=9000): print(f"  ⚠ {name}: port {p} outside 8000-9000")
    if p in ports: print(f"  ✗ {name}: port {p} collides with {ports[p]}"); bad+=1
    ports[p]=name
    if not c.get("spec_url"): print(f"  ✗ {name}: no spec_url"); bad+=1
    # nginx route (glama uses /oauth/mcp; others /oauth/ex/<name>/mcp)
    route = "/oauth/mcp" if name=="glama" else f"/oauth/ex/{name}/mcp"
    if ng and route not in ng and name!="box":
        print(f"  ⚠ {name}: no nginx route {route} (backend-only)")
    # --spec pings the URL the proxy will actually use, so ${VAR:-default}
    # refs have to be expanded first (hygiene still checks the raw manifest).
    if checkspec:
        spec_url = expand(str(c.get("spec_url","")))
        if spec_url.startswith("http"):
            try:
                r=subprocess.run(["curl","-s","-o","/dev/null","-w","%{http_code}","--max-time","8",spec_url],capture_output=True,text=True)
                if r.stdout not in ("200","301","302"): print(f"  ⚠ {name}: spec_url -> HTTP {r.stdout}")
            except Exception as e: print(f"  ⚠ {name}: spec check failed {e}")
# P1 secret hygiene: manifest must carry only ${VAR} refs, never literal keys.
try:
    violations = check_manifest_secrets(d)
    for v in violations:
        print(f"  ✗ secret hygiene: {v}")
        bad += 1
    print(f"  secret hygiene: {len(violations)} violation(s)")
except Exception as e:
    print(f"  ✗ secret hygiene check failed: {e}")
    bad += 1
print(f"\n  {len(d)} servers ({Path(mf).name}) · {len(ports)} unique ports · {bad} hard errors")
sys.exit(1 if bad else 0)
PY
rc=$?
echo "  CLI unit owner (user vs system, look-only is-active/is-enabled):"
python3 "$ROOT/bin/cli_unit_owner.py" --manifest "$MANIFEST" | sed 's/^/    /'
if [ -f "$ROOT/servers.json" ]; then
  echo "  nginx config test:"; sudo nginx -t 2>&1 | tail -1 | sed 's/^/    /'
else
  echo "  (nginx config test skipped — no live servers.json on this host)"
fi
exit $rc
