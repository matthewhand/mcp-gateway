# mcp-gateway — Operations Runbook

Practical procedures for running the gateway. Host-specific secret *locations* are here;
secret *values* live only in `~/.config/mcp-gateway/` and `servers.json` (both gitignored).

## Health & self-heal
- **WebUI:** `https://mcp.example.com/` (also `/status` and `/status.html`) — nginx
  `auth_basic` via `/etc/nginx/.htpasswd`. Live uptime + systemd restart counts. Live restarts /
  port-down rows pulse; stale flap counts stay sorted/red but do not pulse. JSON at
  `/status.json`. Backend is user unit `mcp-status` on `127.0.0.1:8781`.
  Change the password with `sudo htpasswd /etc/nginx/.htpasswd <user>` (MCP/OAuth routes are
  not covered by this file).
  `/status.json` `summary.restarting` is a v1 alias of `summary.live` (pulse count; stale flaps
  do not increment it).
- `./status.sh` — CLI snapshot (services, per-server, containers, certs, disk, memory).
- **Tests** — `uv run pytest tests/`. Live-host audits (systemd units, `servers.json`,
  the oracle-era `~/.config/swarm` layout) are gated behind `MCP_LIVE_HOST=1` (or
  auto-detect via a repo-root `servers.json`; `MCP_LIVE_HOST=0` forces off), so the suite is
  green on dev boxes and still enforces production state on the gateway host. See `tests/live_host.py`.
- `python3 bin/status_collect.py` — same data as the WebUI (add `--json`).
- `mcp-watchdog.timer` runs `bin/watchdog.sh` every 5 min: restarts hung/dead bridges, logs to
  `~/.state/mcp-gateway/watchdog.log`. Check it: `systemctl --user list-timers mcp-watchdog.timer`.

## Memory caps (user systemd drop-ins)
Cgroup `MemoryMax` lives in `~/.config/systemd/user/*.service.d/memory.conf`. Too-low caps
show up as `Memory cgroup out of memory: Killed process … (uv|MainThread|npm exec superg)`
in `~/memwatch/` and a rising `NRestarts` on the status WebUI.

Current floors (2026-08-30, after mcp-openapi-proxy 0.3.4 uvx sat at 487/512 and cgroup-killed glama):

| Pattern | MemoryHigh | MemoryMax |
|---------|------------|-----------|
| `mcp-server@*` (OpenAPI / uvx) | 640M | 768M |
| `mcp-glama` | 640M | 768M |
| `mcp-cli-server@*` | 640M | 768M |
| `mcp-cli-server@acpx-cli` | 896M | 1G |
| `mcp-cli-server@openscad` | 640M | 768M |
| `mcp-server@netbox` / `virustotal` (wordpress-dev disabled 2026-09-06) | 640–700M | 768M |

These are **user-systemd cgroup caps**, not Docker `mem_limit`. Docker stacks (netbox/zep) already have `mem_limit` in their compose overrides. Raising `MemoryMax` does not reserve RAM; it only stops the cgroup OOM killer.

After editing a drop-in: `systemctl --user daemon-reload` then restart **one unit at a time**
(avoid a simultaneous `uvx` stampede). `NRestarts` resets on that restart.

## Autostart (user systemd + linger)
User `chatgpt` has **linger** enabled so units start at boot without an interactive login.

| Kind | Unit pattern | `enable` |
|------|----------------|----------|
| Core | `mcp-oauth`, `mcp-glama`, `mcp-status`, `mcp-watchdog.timer` | yes (user) |
| OpenAPI bridges | `mcp-server@<name>` (from `servers.json`, non-CLI) | yes (user) |
| CLI bridges | `mcp-cli-server@<name>` (openscad, gh-cli, oci-cli, acpx-cli) | **user + linger only** |
| CLI bridges (system copy) | `mcp-cli-server@<name>` in `/etc/systemd/system` | **no** — must stay disabled |
| Swarm API | `open-swarm-oracle` | **no** — discontinued 2026-08-31 (`configs/DISABLED-openapi.md`) |
| Terminal API | `gpt-terminal-plus` | yes |
| nginx | system unit | yes (not user) |

Parked CLIs (`vibe-cli`, `agy-cli`) are **not** in the manifest — see `configs/DISABLED-cli.md`.

Units live in `~/.config/systemd/user/` (`WantedBy=default.target`). After boot:
`systemctl --user list-units 'mcp*' --state=running`.
To re-enable after cloning: copy `systemd/*.service` into that dir (strip `User=` for user mode),
`daemon-reload`, then `enable` each instance as above.

### CLI MCP unit owner (hard-stop)

**Prefer the linger user unit.** CLI MCPs run as `mcp-cli-server@<name>` under
user `chatgpt` (linger so they start at boot). A matching **system** instance of
the same name must stay **disabled**. Both sides bind the same bridge + MCP
ports; if both are active or enabled they fight and flap.

- **Intended owner:** `systemctl --user enable --now mcp-cli-server@<name>`
  (user linger). Do this only when standing up a **new** name — do not restart
  live `acpx-cli` or `gpt-terminal-plus` to land docs/checks.
- **System template install:** copying `systemd/mcp-cli-server@.service` into
  `/etc/systemd/system` (and `daemon-reload`) is fine. **Leave instances
  disabled** wherever a user unit is intended. Do not
  `systemctl enable --now mcp-cli-server@<name>` on the system bus.
- **Checks:** `bin/validate.sh` and `./status.sh` warn when both the user and
  system instances are active or enabled for the same name. Those checks are
  look-only (`systemctl is-active` / `is-enabled`); they never start or stop
  units. No mass restart is required to land this rule.

#### Example: acpx-cli (class of CLI bridges)

| Role | Loopback port | Owner |
|------|----------------|-------|
| MCP (streamable HTTP) | `127.0.0.1:8826` | user `mcp-cli-server@acpx-cli` |
| CLI HTTP bridge | `127.0.0.1:3870` | same user unit (child of the launcher) |

Public path: `/oauth/ex/acpx-cli/mcp`. Ports and unit names only — no secrets
in this example. Same pattern for other CLI names (openscad `:8816`/`:3860`,
gh-cli `:8824`/`:3868`, oci-cli `:8825`/`:3869`).

```bash
# Look-only. Never start/stop/enable/disable as part of this check.
systemctl --user is-active mcp-cli-server@acpx-cli    # expect: active
systemctl --user is-enabled mcp-cli-server@acpx-cli   # expect: enabled
systemctl is-active mcp-cli-server@acpx-cli           # expect: inactive
systemctl is-enabled mcp-cli-server@acpx-cli          # expect: disabled or not-found
```

Do **not** `enable --now` the system instance. Do **not** restart/kill
`acpx-cli` or `gpt-terminal-plus` for this rule. Do **not** re-enable
`open-swarm-oracle`.

## Add / remove a server
- Add: `bin/add-server.sh <name> <spec_url> [port] [-- API_KEY=… …]` (manifest + nginx route + start).
- Validate after any manifest edit: `bin/validate.sh` (`--spec` also pings each spec URL).
  Also warns (look-only) if user and system `mcp-cli-server@<name>` are both
  active or enabled — see **CLI MCP unit owner** above.
- Remove: delete the block from `servers.json`, remove its `/oauth/ex/<name>/mcp` nginx block,
  `sudo nginx -t && systemctl reload nginx`, `sudo systemctl disable --now mcp-server@<name>`.
- **Disable (keep the entry, stop + deprioritise):** set `"enabled": false` on the server's
  block, then `sudo systemctl disable --now mcp-server@<name>`. `bin/watchdog.sh`, `install.sh`,
  and status all honor the flag (watchdog/install skip it; the unit stays stopped). Re-enable
  later: set `"enabled": true` (or delete the key) and `sudo systemctl enable --now mcp-server@<name>`.
  Example: `wordpress-dev` disabled 2026-09-06 — its `servers.json` entry keeps the
  `${WP_DEV_API_KEY}` refs, so `check-env-refs.sh` reports that ref missing until the key is
  restored or the entry removed; that warning is expected while the bridge is disabled.
- **CLI / endpoint builder UI** (copy-paste only, no auto-write): open `webui/gateway-builder.html` in a
  browser. Run generator unit tests: `python3 webui/test_generators.py` (needs `node` + `cli_bridge`).
  CLI bridges use `mcp-cli-server@<name>` (`bin/launch-cli-server.sh`), not `mcp-server@`.
- **CLI MCP goal (acceptance + re-verify):** `docs/goals/cli-mcp-gateway.md` — live set, ports, checklist.
- **POST /run (CLI bridges):** auto-exposed by `cli_bridge` (`allow_run`, default on). Body
  `{"args":["issue","list",…], "working_directory":"…"}` runs `command + args` for that bridge only.
  Args must start with a defined operation argv prefix (e.g. `["issue","list"]`). Config knobs:
  `allow_run`, `run_require_known_prefix`. Env:
  - `CLI_BRIDGE_ALLOW_ARBITRARY_COMMAND=1` — also accept absolute `command` override (any binary)
  - `CLI_BRIDGE_DISABLE_RUN=1` — hide `/run` entirely
  Include `/run` in each CLI server’s `TOOL_WHITELIST`. Tests: `python3 bin/test_cli_bridge_run.py`.

## Restart / recover
- One bridge: `sudo systemctl restart mcp-server@<name>`.
- OAuth/glama/all: `sudo systemctl restart mcp-oauth mcp-glama 'mcp-server@*'`.
- A container stack: `cd <stack dir> && docker compose up -d` (applies mem caps + restart policy).
- box (manual, key-gated): only start when `BOX_API_KEY` is set and unexpired.
  Empty/expired key → launcher **skips the proxy** (holds without binding `:8810`)
  and status marks **UNHEALTHY** (`short-lived Box token; refresh BOX_API_KEY`).
  `sudo systemctl start mcp-server@box` … `stop` when done.

## Slow OpenAPI / empty tools
`bin/launch-server.sh` **preflights** each `spec_url` and writes to
`~/.state/mcp-gateway/servers.log`:

- `WARN` if fetch ≥ **10s** (`MCP_SPEC_SLOW_WARN_SEC`) — clients often give up at 30–60s on
  `initialize` / `tools/list` while the proxy is still loading the spec.
- `CRIT` if fetch ≥ **30s** (`MCP_SPEC_SLOW_CRIT_SEC`) — cache the schema; clients will not wait.
- Local `file://` specs ≥ ~1MB also get a size WARN (first tools/list can still be slow).

Example (NetBox live `/api/schema/` was ~70s / ~4MB): cache a whitelist min spec under `specs/`
and set `spec_url` to `file:///…/specs/netbox-min.yaml` (see current `netbox` entry).

```bash
grep -E 'WARN|CRIT|OPENAPI_SPEC' ~/.state/mcp-gateway/servers.log | tail -40
```

## Secret rotation  ⚠️ do periodically
1. **Upstream API keys** (per server): regenerate at the provider, then put the new value in
   `~/.config/swarm/.env` (or `mcp-gateway/.env`) under the `${VAR}` the manifest references.
   **`servers.json` must only ever carry `${VAR}` refs, never literal keys** (enforced by
   `bin/validate.sh` → `bin/secret_hygiene.py`; `bin/check-env-refs.sh` verifies refs resolve).
   Non-secret host knobs may use the `${VAR:-default}` form instead (see
   *gpt-terminal-plus upstream configuration*); those always resolve.
   Then `bin/validate.sh`, `sudo systemctl restart mcp-server@<name>`, and verify a tool call via
   `./status.sh safe-probe <name>`. Adding a server: `bin/add-server.sh <name> <spec> -- API_KEY=…`
   writes the ref to the manifest and the value to `mcp-gateway/.env` for you.
2. **OAuth client_secret** (`~/.config/mcp-gateway/oauth.conf`): set a new value,
   `sudo systemctl restart mcp-oauth`, update every client's connector. (Existing JWTs keep validating
   until expiry — they're signed by `jwt_secret`, not the client_secret.)
3. **JWT signing secret** (`~/.config/mcp-gateway/oauth.conf` `jwt_secret`): rotating it **invalidates all
   live access tokens** — clients re-auth automatically. Restart `mcp-oauth`.
4. **Static bearer** (`~/.config/mcp-gateway/bearer-token`): update the value AND the matching
   `if ($http_authorization != "Bearer …")` line in `nginx/mcp.conf`, `sudo nginx -t && reload`.
5. **Grok path-secret** (`/grok/<secret>` in `nginx/mcp.conf` + `~/.config/mcp-gateway/grok-url`):
   change both, reload nginx, reconfigure the Grok connector.
5b. **DCR client store** (`~/.state/mcp-gateway/oauth-clients.json`, override `$MCP_OAUTH_CLIENTS`):
   public PKCE registrations (no `client_secret`). Deleting the file and restarting
   `mcp-oauth` forgets dynamically registered clients. Never commit this file.
   Registration is rate-limited (`MCP_OAUTH_REGISTER_LIMIT` / `MCP_OAUTH_REGISTER_WINDOW`,
   default 10 per IP per 60s). Live nginx needs `location = /oauth/register` → `:8780/register`
   (see `nginx/mcp.conf.example`). CIMD URL `client_id`s are fetched at
   authorize/token time (HTTPS only; private/link-local targets blocked) and
   cached in memory per HTTP cache headers — they are **not** stored in this
   file. Rate-limit: `MCP_OAUTH_CIMD_LIMIT` / `MCP_OAUTH_CIMD_WINDOW`
   (default 20 per IP per 60s).
5c. **OAuth MCP 401 challenge (RFC 9728 / Gemini Spark):** live `nginx/mcp.conf`
   (gitignored) must match `nginx/mcp.conf.example`:
   - `location @mcp_oauth_401` returning `401` JSON `{"error":"invalid_token"}`
     plus `WWW-Authenticate: Bearer realm="OAuth", resource_metadata="https://$host/.well-known/oauth-protected-resource$request_uri", error="invalid_token"`
   - `error_page 401 = @mcp_oauth_401;` immediately after `auth_request /oauth-validate;`
     on **every** `/oauth/mcp` and `/oauth/ex/<name>/mcp` location (not `/mcp`,
     not `/grok/…`, not status htpasswd)
   - path-scoped `/.well-known/oauth-protected-resource` (unauthenticated;
     `resource` = `https://$host` + suffix)
   After editing: `sudo nginx -t && sudo systemctl reload nginx`.
   Check (must be JSON, not nginx HTML):

   ```bash
   curl -sS -D - -o /tmp/mcp401.json -X POST \
     https://mcp.example.com/oauth/ex/acpx-cli/mcp \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
   ```

   Do **not** expose `:8815` / `:3100`. This is discovery parity, not a
   protocol hard-cut — grok / DCR / CIMD stay as they are.
6. **Box short-lived token** (`BOX_API_KEY` only — never commit the value):
   1. Create a new Box developer / CCG token in the Box developer console.
   2. Set `BOX_API_KEY` in `mcp-gateway/.env` **or** write the token only to
      `~/.config/mcp-gateway/box-api-key` (optional override: `MCP_BOX_KEY_FILE`).
   3. `bin/check-env-refs.sh` (prints names + lengths, not values).
   4. `python3 bin/box_key.py --check` — exit 0 means the gate will allow launch.
   5. `systemctl --user start mcp-server@box` (manual). Confirm with
      `./status.sh safe-probe apisguru` (not `safe-probe gpt-terminal-plus`
      from inside Terminal Plus) or the status WebUI row — **do not** require
      `:8810` to stay up; stop the unit when finished.
> Hardening goal (ROADMAP): move `servers.json` keys to `${ENV_VAR}` refs so the manifest holds no literals.

## Deadlock-safe probing
`gpt-terminal-plus` listens on `127.0.0.1:8815` (upstream API `127.0.0.1:3100`
by default — see *gpt-terminal-plus upstream configuration* below).
A **foreground** `curl` of `:8815` (or the public OAuth MCP URL) from inside a
Terminal Plus tool call **deadlocks** that worker: the probe waits on the
request the worker is already serving.

Use the gateway probe — never hand-curl those URLs from a nested tool call:

```bash
./status.sh safe-probe <name>     # or: bin/safe-probe <name>
bin/safe-probe --plan gpt-terminal-plus   # show the plan; no curl
```

- **Native** instances: `GET http://127.0.0.1:<port>/healthz` (sister work
  [mcp-openapi-proxy#66](https://github.com/matthewhand/mcp-openapi-proxy/issues/66);
  JSON `{"ok":true,"name":…,"port":…}`). No `initialize`, no `POST /mcp`.
- **Fallback** if `/healthz` is missing (0.3.4 / supergateway / proxy not yet
  patched): listen-only via `ss -tlnH` on `127.0.0.1:<port>`. Still no
  `POST /mcp`.
- **`name=gpt-terminal-plus`** (or any probe whose port equals the caller’s
  `MCP_PORT` / `MCP_SERVER_NAME` port): **do not** foreground-curl `:8815`.
  `safe-probe` switches to a **different instance** (prefer `glama :8765` /
  `apisguru`) and starts a **background** `curl` to `/tmp/mcp-safe-probe-<name>.out`.
- Operator override (interactive tty, not a nested tool): 
  `SAFE_PROBE_ALLOW_FOREGROUND=1 bin/safe-probe gpt-terminal-plus`.
- **Never** probe `https://mcp.example.com/oauth/ex/gpt-terminal-plus/mcp`
  from inside Terminal Plus.

## gpt-terminal-plus upstream configuration
`servers.json` no longer hardcodes the terminal API URL. The `gpt-terminal-plus` entry carries
`"${GPT_TERMINAL_PLUS_URL:-http://localhost:3100}"` as `env.SERVER_URL_OVERRIDE` and
`"${GPT_TERMINAL_PLUS_URL:-http://localhost:3100}/openapi.json"` as `spec_url`, so an unset
variable means exactly today's `http://localhost:3100` (127.0.0.1:3100).

| Var | Effect |
|-----|--------|
| `GPT_TERMINAL_PLUS_URL` | full upstream base URL (e.g. `http://localhost:4100`). Used when set and non-empty. |
| `GPT_TERMINAL_PLUS_PORT` | used to build `GPT_TERMINAL_PLUS_URL` by setting it in the same env file — `GPT_TERMINAL_PLUS_PORT=4100` alone resolves to `http://localhost:4100`. Default `3100`. |

Where to set them:

- **Gateway side** (what the bridge connects to): `/home/chatgpt/mcp-gateway/.env` — sourced by
  `bin/launch-server.sh` before the manifest is expanded, so no manifest edit and no restart of
  anything but `mcp-server@gpt-terminal-plus`.
- **Service side** (what the API listens on): the `mcp-server@gpt-terminal-plus.service` drop-in
  (`/etc/systemd/system/mcp-server@gpt-terminal-plus.service.d/*.conf`) or, for the upstream unit,
  `/etc/systemd/system/gpt-terminal-plus.service.d/*.conf` with `EnvironmentFile=-/etc/default/gpt-terminal-plus`
  (`PORT=3100` there must match `GPT_TERMINAL_PLUS_PORT` on the gateway side).

After changing either side: `sudo systemctl restart mcp-server@gpt-terminal-plus`, then
`grep gpt-terminal-plus ~/.state/mcp-gateway/servers.log | tail`. `bin/validate.sh --spec` and
`bin/check-env-refs.sh` both expand refs before checking, so a `${VAR:-default}` value is never
reported as a missing variable.

## TLS
- Cert lineage `mcp` (covers `mcp.` + legacy `glama-bridge.`), auto-renews via certbot.
- Check: `sudo certbot certificates`. Force renew: `sudo certbot renew --force-renewal`.

## fail2ban
- Jail name: `mcp-gateway-auth` (`/etc/fail2ban/jail.d/mcp-gateway.conf`; filter
  `filter.d/mcp-gateway-auth.conf`). Matches 401/403 on `/(?:mcp|oauth)` only — status
  htpasswd 401s do not ban MCP clients.
- Status: `sudo fail2ban-client status mcp-gateway-auth`. Unban: `… set mcp-gateway-auth unbanip <ip>`.
- `ignoreip` includes home ISP `219.90.166.149` (jail `mcp-gateway.conf` also keeps
  `121.44.0.0/15`; host-wide list is `jail.d/home-isp.local`).
- INPUT jumps exist for `nginx-http-auth` and `mcp-gateway-auth` even with zero bans
  (`jail.d/actionstart-always.local`).
- Local WordPress: only the wanted `wp-via-openapi` stack (`/mnt/models/projects/wp-via-openapi`);
  the Awardspace `wordpress-dev` bridge is disabled (2026-09-06).

## Engine version / transport
Default is the **newer native workflow** (prefer this):

| Mode | How | Protocol |
|------|-----|----------|
| **native** (default) | `MCP_TRANSPORT=streamable-http` via `uv run` in `mcp-openapi-proxy` **0.4.0** (`mcp>=2,<3`) | MCP **2026-07-28** — **stateless `POST /mcp`**, no `Mcp-Session-Id`, **no nginx sticky sessions** |
| **supergateway** escape hatch | `MCP_GATEWAY_HTTP=supergateway` wraps stdio (no `--stateful`) | older HTTP wrapper; keep this path working |

Source tree: `MCP_OPENAPI_PROXY_SRC` (default `~/mcp-openapi-proxy`).
Restart bridges after changing mode or pins. Do **not** re-add sticky
sessions; do **not** expose `:8815` or `:3100` on `0.0.0.0`.

### Rollback (escape hatch)
To put bridges back on the last `mcp<2` release:

1. Pin **`mcp-openapi-proxy~=0.3.4`** and **`mcp>=1.2.0,<2`** in the
   launcher / `uv` env (0.3.4 was the last release pinned to `mcp<2`).
2. Set `MCP_GATEWAY_HTTP=supergateway` so `launch-server.sh` /
   `launch-glama.sh` / CLI launchers wrap stdio again.
3. Restart one unit at a time. Native `POST /mcp` + `/healthz` are 0.4.0
   features; under 0.3.4 use listen-only `safe-probe` fallback.

Do **not** revert the repo default off 0.4.0 unless rolling back a host.

## Disaster recovery
- Fresh host: `git clone` the repo, restore `servers.json` + `~/.config/mcp-gateway/*` from your secret
  store, create `nginx/mcp.conf` from `.example` + secrets, run `install.sh`.
- Pre-consolidation backups: `/mnt/models/archive/`.

## Logs, disk pressure & rotation

### Why this matters
A full root filesystem used to **kill** gateway units: systemd
`StandardOutput=append:…/oauth.log` makes the first write on ENOSPC fatal
(Python often exits `120`). OAuth then disappeared behind nginx `502`s even
though the code was fine.


### Log directory location
Gateway logs live under **`~/.state/mcp-gateway/`** on the **root filesystem**.
Do **not** put them under `~/.local/state` on this host — `~/.local` is a symlink to
`/mnt/models/.local`, and `/mnt/models` routinely fills up (camera archives), which
is what originally ENOSPC-killed oauth.

### Runtime protection (preferred path)
Units no longer append logs via systemd. They run under
`bin/safe-log-run.py LOG -- cmd…`:

| Layer | What it does |
|-------|----------------|
| `bin/safe-log-run.py` | Pipes child stdout/stderr to the log file; on **ENOSPC** truncates that log once and retries, else **drops the chunk** — child keeps running. Soft-caps each **open** log at `MCP_LOG_SOFT_MAX_MB` (default 50). Does not delete other files. |
| `bin/disk-guard.sh` | **No rotation. No `rm`.** On **CRIT** disk only: truncate active `STATE/*.log` in place so writers can continue. WARN = log only. |
| **logrotate** | **Only** component that rotates and deletes old log generations. |
| `bin/watchdog.sh` | Runs disk-guard first every 5 min; restarts dead oauth/glama/bridges. |
| `oauth-server.py` | Soft stderr + ignore client `BrokenPipeError`. |
| journald | Wrapper messages (`SyslogIdentifier=mcp-oauth` etc.). |

`disk-guard` self-test: `bin/disk-guard.sh --self-test` (must refuse `/`, `$HOME`, empty, relative).

### logrotate (owns rotation + old file deletion)
Config: `/etc/logrotate.d/mcp-gateway` (source: `configs/logrotate-mcp-gateway`):

- **daily** (and earlier if any file exceeds **50M**)
- keep **7** compressed generations (`dateext`)
- **copytruncate** (works with long-lived writers / safe-log-run)

```bash
sudo cp configs/logrotate-mcp-gateway /etc/logrotate.d/mcp-gateway
sudo logrotate -d /etc/logrotate.d/mcp-gateway   # dry-run
```

Check last run: `grep mcp-gateway /var/lib/logrotate/status`

Do **not** reintroduce DIY “keep N rotated” reclaim in app scripts — that path deleted home/project files once.

