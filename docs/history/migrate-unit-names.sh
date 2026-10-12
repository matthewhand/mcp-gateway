#!/bin/bash
# One-time migration: legacy unit names -> clean names.
#   glama-oauth  -> mcp-oauth
#   glama-bridge -> mcp-glama
#   mcp-example@<name> -> mcp-server@<name>
set -uo pipefail
P=/home/chatgpt/mcp-gateway
echo "[1] install clean-named unit files"
sudo cp "$P/systemd/mcp-oauth.service" "$P/systemd/mcp-glama.service" "$P/systemd/mcp-server@.service" /etc/systemd/system/
sudo systemctl daemon-reload

# capture active server instances BEFORE we touch anything
mapfile -t INSTANCES < <(systemctl list-units 'mcp-example@*' --all --no-legend | awk '{print $1}' | sed -E 's/mcp-example@(.*)\.service/\1/')
echo "[2] instances to migrate: ${INSTANCES[*]}"

migrate_simple() { # old new
  local old="$1" new="$2"
  sudo systemctl disable --now "$old" >/dev/null 2>&1
  sudo systemctl enable --now "$new"  >/dev/null 2>&1
  echo "   $old -> $new : $(systemctl is-active "$new")"
}
echo "[3] oauth + glama"
migrate_simple glama-oauth.service  mcp-oauth.service
migrate_simple glama-bridge.service mcp-glama.service

echo "[4] servers (preserve enabled/disabled state; box stays manual)"
for n in "${INSTANCES[@]}"; do
  was_enabled=$(systemctl is-enabled "mcp-example@$n" 2>/dev/null)
  sudo systemctl disable --now "mcp-example@$n" >/dev/null 2>&1
  if [ "$n" = box ] || [ "$was_enabled" != enabled ]; then
    sudo systemctl start "mcp-server@$n" >/dev/null 2>&1   # box: start only if it was running; otherwise leave
    [ "$n" = box ] && sudo systemctl stop "mcp-server@$n" >/dev/null 2>&1   # box manual: keep stopped+disabled
    echo "   $n -> mcp-server@$n (left disabled/manual)"
  else
    sudo systemctl enable --now "mcp-server@$n" >/dev/null 2>&1
    echo "   $n -> mcp-server@$n : $(systemctl is-active mcp-server@$n)"
  fi
done

echo "[5] remove legacy unit files"
sudo rm -f /etc/systemd/system/glama-oauth.service /etc/systemd/system/glama-bridge.service /etc/systemd/system/mcp-example@.service
sudo rm -f /etc/systemd/system/multi-user.target.wants/mcp-example@*.service 2>/dev/null
sudo systemctl daemon-reload
echo "[done]"
