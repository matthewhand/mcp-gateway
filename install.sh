#!/bin/bash
# install.sh — deploy the MCP gateway on a fresh host.
# Idempotent-ish; safe to re-run. Does NOT overwrite servers.json or secrets.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "[1/5] prerequisites: node/npx, uv/uvx, python3, nginx, certbot"
for b in npx uvx python3 nginx; do command -v "$b" >/dev/null || echo "  MISSING: $b"; done

echo "[2/5] servers.json + secrets present?"
[ -f "$HERE/servers.json" ] || { echo "  copy servers.example.json -> servers.json and fill keys"; }
[ -f "$HOME/.config/mcp-gateway/oauth.conf" ] || echo "  MISSING ~/.glama-oauth.conf (OAuth client_id/secret/jwt_secret)"
[ -f "$HOME/.config/mcp-gateway/bearer-token" ] || echo "  MISSING ~/.glama-bridge-token (static bearer)"

echo "[3/5] systemd units -> /etc/systemd/system (clean names)"
sudo cp "$HERE/systemd/mcp-oauth.service"   /etc/systemd/system/
sudo cp "$HERE/systemd/mcp-glama.service"   /etc/systemd/system/
sudo cp "$HERE/systemd/mcp-server@.service" /etc/systemd/system/
sudo systemctl daemon-reload

echo "[4/5] nginx vhost -> sites-enabled (real conf is local/untracked; see README.local.md)"
[ -f "$HERE/nginx/mcp.conf" ] || { echo "  create nginx/mcp.conf from nginx/mcp.conf.example + your TLS/secrets"; exit 1; }
sudo ln -sf "$HERE/nginx/mcp.conf" /etc/nginx/sites-enabled/mcp.conf
sudo nginx -t && sudo systemctl reload nginx

echo "[5/5] enable services (one mcp-server@<name> per key in servers.json)"
sudo systemctl enable --now mcp-oauth mcp-glama
for s in $(python3 -c "import json;print(' '.join(json.load(open('$HERE/servers.json'))))"); do
  [ "$s" = box ] && { echo "  skip box (manual-only)"; continue; }
  python3 -c "import json,sys;d=json.load(open('$HERE/servers.json'));sys.exit(0 if str(d.get('$s',{}).get('enabled',True)).lower()=='false' else 1)" \
    && { echo "  skip $s (enabled:false)"; continue; }
  sudo systemctl enable --now "mcp-server@$s"
done
echo "Done. Check ./status.sh"
