#!/bin/bash
set -uo pipefail
echo "=== post-boot check $(date -Iseconds) ==="
echo "Linger=$(loginctl show-user chatgpt -p Linger --value 2>/dev/null)"
echo
echo "-- enabled core --"
systemctl --user is-enabled mcp-oauth mcp-glama mcp-status mcp-watchdog.timer gpt-terminal-plus 2>&1
echo
echo "-- running mcp* --"
systemctl --user list-units 'mcp*' 'open-swarm*' 'gpt-terminal*' --state=running --no-pager 2>&1
echo
echo "-- failed --"
systemctl --user --failed --no-pager 2>&1
echo
echo "-- sample ports --"
for p in 8765 8780 8781 8804 8809 8816 8824 8826; do
  ss -tln | grep -q ":$p\b" && echo "OK :$p" || echo "NO :$p"
done
echo
echo "-- nginx --"
systemctl is-active nginx 2>&1
