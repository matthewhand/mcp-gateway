# mcp-gateway — Operator Card

One-page ops card for the CoS / MCP Bot: FQDN, OAuth issuer, unit map, status, disable
flags, hard-stops. **Full detail: [`RUNBOOK.md`](RUNBOOK.md) and [`CLIENTS.md`](CLIENTS.md).**

## Public endpoints

- **FQDN / OAuth issuer:** `https://mcp.example.com`
- **MCP path pattern:** `https://mcp.example.com/oauth/ex/<name>/mcp`
- **glama OAuth:** `https://mcp.example.com/oauth/mcp` (also bearer `/mcp`, path-secret `/grok/<secret>`)
- **OAuth AS:** nginx-fronted; `bin/oauth-server.py` on `127.0.0.1:8780`
- **Discovery:** `/.well-known/oauth-authorization-server` (PRM: `/.well-known/oauth-protected-resource/<request-uri>`)
- **Status WebUI:** `https://mcp.example.com/` (htpasswd; also `/status`, `/status.html`, `/status.json`) — backend user unit `mcp-status` on `127.0.0.1:8781`

## Unit map

| Unit / pattern | Role | Prefer | Notes |
|---|---|---|---|
| `mcp-oauth` | OAuth2 AS (`:8780`) | **user** | `systemctl --user` |
| `mcp-glama` | glama bridge (`:8765`) | **user** | |
| `mcp-status` | status WebUI (`:8781`) | **user** | |
| `mcp-server@<name>` | OpenAPI MCP bridge | **user** | e.g. `gpt-terminal-plus` `:8815` (upstream `:3100`) |
| `mcp-cli-server@<name>` | CLI bridge | **user + linger** | `openscad` `:8816`/`:3860`, `gh-cli` `:8824`/`:3868`, `oci-cli` `:8825`/`:3869`, `acpx-cli` `:8826`/`:3870` |
| `mcp-cli-server@*` (system copy) | must stay **disabled** | system | ports fight the user unit: acpx-cli bridge `:3870` / MCP `:8826` |
| `nginx`, `fail2ban` | edge / ban | **system** | `systemctl status nginx` |
| `open-swarm-oracle` | discontinued | system/user | do **not** re-enable without Matthew |

User units live in `~/.config/systemd/user/` and run under **linger** (start at boot).
**Disable flag:** `"enabled": false` in `servers.json` — `watchdog.sh`, `install.sh` and status
all honor it; the unit stays stopped. Re-enable by removing the flag then enabling the unit.

## ⛔ Hard-stops (read before acting)

> **Do not restart/kill `acpx-cli` or `gpt-terminal-plus` without pinging MCP Bot.**
> **Do not re-enable `open-swarm-oracle` without Matthew.**
> **CLI MCPs run as linger user `mcp-cli-server@<name>`.** Prefer that user unit.
> The matching **system** instance must stay **disabled** — they fight for the
> same bridge/MCP ports (acpx-cli class: bridge `:3870` / MCP `:8826`). Installing
> the system template must leave those instances disabled where a user unit is
> intended. `bin/validate.sh` / `./status.sh` warn if both are active or enabled
> (look-only `is-active` / `is-enabled`; never start/stop).
> **No secrets in git** — locations only: `~/.config/mcp-gateway/oauth.conf`,
> `~/.config/mcp-gateway/bearer-token`, `servers.json` `${VAR}` refs, `~/.state/mcp-gateway/`.
> **Never foreground-curl `:8815` from inside Terminal Plus** — use `./status.sh safe-probe <name>`.

## Status

```bash
./status.sh                                  # full CLI snapshot
./status.sh safe-probe <name>                # loopback /healthz (never :8815 from Terminal Plus)
systemctl --user status mcp-oauth mcp-glama mcp-status
systemctl --user status mcp-server@apisguru  # representative OpenAPI bridge
systemctl --user status 'mcp-cli-server@acpx-cli'
systemctl status nginx                       # system unit
```

## SSH checklist (READ-ONLY)

```bash
# OAuth AS discovery (JSON; issuer must be the public FQDN)
curl -sS https://mcp.example.com/.well-known/oauth-authorization-server

# 401 JSON challenge (not nginx HTML) — proves nginx + OAuth AS path
curl -sS -D - -o /dev/null -X POST \
  https://mcp.example.com/oauth/ex/acpx-cli/mcp \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# Listeners: oauth/status on localhost, bridges on 127.0.0.1
ss -tlnH | grep -E ':(8780|8781|8765|8815|8826)\b'

# Unit states — active / enabled (look-only; never start/stop)
systemctl --user is-active mcp-oauth mcp-glama mcp-status
systemctl --user is-active mcp-server@apisguru 'mcp-cli-server@acpx-cli'
systemctl is-active nginx fail2ban
systemctl --user is-enabled 'mcp-cli-server@acpx-cli'   # user linger unit is the intended owner
systemctl is-active 'mcp-cli-server@acpx-cli'           # system copy: expect inactive
systemctl is-enabled 'mcp-cli-server@acpx-cli'          # system copy: expect disabled / not-found
# Dual-owner warn (same look-only checks, all CLI names):
bin/validate.sh
./status.sh
```

## Pointers

- Full procedures, secret rotation, recovery: [`RUNBOOK.md`](RUNBOOK.md).
- Client URLs, OAuth/DCR/CIMD wiring: [`CLIENTS.md`](CLIENTS.md).
- This card is a quick reference only — **it does not replace RUNBOOK.md or CLIENTS.md.**
