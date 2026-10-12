# Disabled OpenAPI servers

## Discontinued (removed from the manifest)

Entries removed from `servers.json` / `servers.example.json`; matching
`mcp-server@<name>` units stopped and disabled; nginx `/oauth/ex/<name>/mcp`
routes removed. **Do not re-enable** without the operator.

| Name | Was port | Status |
|------|----------|--------|
| **open-swarm-oracle** | 8814 (Django upstream `:8001`) | **disabled 2026-08-31** — Open Swarm discontinued on this host. Django unit `open-swarm-oracle.service` stays disabled. Historical XDG notes: `docs/OPEN-SWARM-XDG.md`. Nginx snippet retained: `nginx/open-swarm-oracle.location.conf.snippet`. |

## Disabled (entry kept, `enabled: false`)

Kept in `servers.json` / `servers.example.json` so the shape and `${VAR}` refs
survive. Watchdog, `install.sh`, and status skip these. Unit stays stopped.

| Name | Port | Status |
|------|------|--------|
| **wordpress-dev** | 8817 | **disabled 2026-09-06** — Awardspace live `wp-json` (not a local WP container). Entry kept with `${WP_DEV_API_KEY}` and `IGNORE_SSL_TOOLS=true` (Awardspace shared-host TLS mismatch; see `specs/wordpress-dev.json`). `check-env-refs.sh` flagging the key ref is expected while disabled. |

Checkout `~/open-swarm` and `~/.config/swarm/` were **not** deleted.

## Re-enable (operator-gated — do not do this from a docs/inventory PR)

`open-swarm-oracle` is discontinued. Do **not** re-enable without Matthew.

```bash
# restore servers.json + servers.example.json + nginx route from git / snippet
systemctl --user enable --now open-swarm-oracle.service          # Django :8001
# wait until http://127.0.0.1:8001/api/schema/ returns YAML
systemctl --user enable --now mcp-server@open-swarm-oracle      # MCP :8814
sudo nginx -t && sudo systemctl reload nginx
```

`wordpress-dev` (kept, disabled): set `"enabled": true` (or remove the flag) and
`systemctl --user enable --now mcp-server@wordpress-dev` — only when the operator
wants the Awardspace bridge back.
