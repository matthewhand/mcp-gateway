# Open Swarm XDG layout (operator)

> **Discontinued on this host (2026-08-31).** `open-swarm-oracle` is not in
> `servers.json`, `mcp-server@open-swarm-oracle` is disabled, and nginx no longer
> proxies `/oauth/ex/open-swarm-oracle/mcp`. See `configs/DISABLED-openapi.md`.
> This page is kept as the historical wiring notes.

## Canonical paths

| Role | Path |
|------|------|
| Config | `~/.config/swarm/swarm_config.json` |
| Secrets | `~/.config/swarm/.env` |
| Library / teams stores | `~/.config/swarm/blueprint_library.json`, `teams.json` |
| Responses | `~/.local/share/swarm/responses` |
| Community blueprints | `~/.local/share/swarm/blueprints` |
| Launchers | `~/.local/share/swarm/bin` |
| Archive (stale backups) | `~/.local/share/swarm/archive` |

Code checkout remains `~/open-swarm` → open-swarm-mcp (venv + `manage.py` only).

## Unit (`open-swarm-oracle.service`)

```
EnvironmentFile=-%h/.config/swarm/.env
Environment=SWARM_CONFIG_PATH=%h/.config/swarm/swarm_config.json
Environment=SWARM_RESPONSES_DIR=%h/.local/share/swarm/responses
```

Loaders (`swarm.utils.dotenv_load.load_swarm_dotenv`) also pull XDG `.env` then checkout `.env` as fallback. **Process env already set (unit) wins.**

## MCP gateway bridge

| Item | Value |
|------|--------|
| Server name | `open-swarm-oracle` |
| Port | `8814` |
| Spec | `http://127.0.0.1:8001/api/schema/` (YAML) |
| API key env | `OPEN_SWARM_API_KEY` (Bearer = unit `API_AUTH_TOKEN`) |

Set in `mcp-gateway/.env`:

```bash
OPEN_SWARM_API_KEY=…same as API_AUTH_TOKEN…
```

## Regression tests

```bash
cd ~/mcp-gateway && python3 -m pytest tests/test_xdg_swarm_config_audit.py -v
```
