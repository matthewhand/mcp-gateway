#!/bin/bash
# add-server.sh <name> <spec_url> [port] [-- env KEY=VAL ...]
# Adds one MCP server end-to-end: appends to servers.json, scaffolds the nginx
# OAuth route, validates, starts the systemd unit, and verifies it binds.
# Idempotent-ish; refuses if the name already exists. Run as the gateway owner.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MANIFEST="$ROOT/servers.json"
NGINX="$ROOT/nginx/mcp.conf"
# Host-specific public URL from local config (gitignored); portable default.
[ -f "$ROOT/.env.local" ] && { set -a; . "$ROOT/.env.local"; set +a; }
PUBLIC_URL="${MCP_PUBLIC_URL:-https://mcp.example.com}"

NAME="${1:?usage: add-server.sh <name> <spec_url> [port] [-- KEY=VAL ...]}"
SPEC="${2:?need spec_url (https://… or file:///…)}"
shift 2
PORT="${1:-}"; [[ "${PORT:-}" =~ ^[0-9]+$ ]] && shift || PORT=""
ENVPAIRS=(); [ "${1:-}" = "--" ] && { shift; ENVPAIRS=("$@"); }

# pick next free port in 8801-8899 if not given
if [ -z "$PORT" ]; then
  PORT=$(python3 -c "
import json; d=json.load(open('$MANIFEST')); used={c['port'] for c in d.values()}
print(next(p for p in range(8801,8900) if p not in used))")
fi
echo "→ adding '$NAME' on :$PORT (spec: $SPEC)"

python3 - "$MANIFEST" "$NAME" "$SPEC" "$PORT" "${ENVPAIRS[@]:-}" <<'PY'
import json,sys
mf,name,spec,port,*envp=sys.argv[1:]
d=json.load(open(mf))
if name in d: sys.exit(f"  ✗ '{name}' already exists — edit servers.json directly")
env={}
for kv in envp:
    if kv and "=" in kv:
        k=kv.split("=",1)[0]
        # P1 env-ref hardening: the manifest only ever carries ${VAR} refs; the
        # real value goes to $ROOT/.env (appended below) and is expanded at launch.
        env[k]=f"${{{k}}}"
d[name]={"port":int(port),"spec_url":spec,**({"env":env} if env else {"env":{}})}
json.dump(d,open(mf,"w"),indent=2)
print(f"  ✓ manifest updated (env keys stored as ${{VAR}} refs)")
PY

# Stash the real values in the gateway .env (sourced by launch-server.sh). Never
# overwrite an existing key — re-run add-server (or edit .env) when rotating.
ENV_OUT="$ROOT/.env"; touch "$ENV_OUT"
for kv in "${ENVPAIRS[@]:-}"; do
  [[ "$kv" == *=* ]] || continue
  k="${kv%%=*}"; v="${kv#*=}"
  if grep -q "^${k}=" "$ENV_OUT"; then
    echo "  ⚠ ${k} already set in $ENV_OUT — kept existing value (rotate by editing .env)"
  else
    # %q shellsafe-quotes for `source` consumers (launch-server.sh / check-env-refs.sh)
    printf '%s=%q\n' "$k" "$v" >> "$ENV_OUT"
    echo "  ✓ ${k} written to $ENV_OUT (manifest references \${${k}})"
  fi
done

# scaffold the nginx OAuth route (before the catch-all `location /`)
if ! grep -q "/oauth/ex/$NAME/mcp" "$NGINX"; then
  python3 - "$NGINX" "$NAME" "$PORT" <<'PY'
import sys
f,name,port=sys.argv[1],sys.argv[2],sys.argv[3]
s=open(f).read()
block=f"""    location /oauth/ex/{name}/mcp {{
        auth_request /oauth-validate;
        error_page 401 = @mcp_oauth_401;
        rewrite ^/oauth/ex/{name}/mcp(/.*)?$ /mcp break;
        proxy_pass http://127.0.0.1:{port};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Connection '';
        proxy_buffering off;
        proxy_read_timeout 3600s;
    }}
"""
anchor="    location / { return 404; }"
s=s.replace(anchor, block+anchor, 1)
open(f,"w").write(s); print("  ✓ nginx route scaffolded")
PY
  sudo nginx -t && sudo systemctl reload nginx && echo "  ✓ nginx reloaded"
fi

sudo systemctl enable --now "mcp-server@$NAME" >/dev/null 2>&1
for i in $(seq 1 15); do ss -tlnH | grep -q ":$PORT" && break; sleep 1; done
if ss -tlnH | grep -q ":$PORT"; then
  echo "  ✓ mcp-server@$NAME up on :$PORT"
  echo "  → public URL: $PUBLIC_URL/oauth/ex/$NAME/mcp"
  echo "  (add upstream API_KEY to servers.json env if the API needs auth, then restart)"
else
  echo "  ✗ did not bind — check: journalctl -u mcp-server@$NAME / docs/PROJECT-LOG troubleshooting"
fi
