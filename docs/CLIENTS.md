# Connecting a client to the gateway

Every server is reachable at `https://mcp.example.com` with one of three auth styles.
For most clients you only need: **server URL + `client_id` + `client_secret`** (OAuth auto-discovers the rest).

## Credentials
| Field | Value / location |
|---|---|
| OAuth `client_id` | `grok` |
| OAuth `client_secret` | `~/.config/mcp-gateway/oauth.conf` |
| OAuth scope | `mcp` |
| Discovery | `https://mcp.example.com/.well-known/oauth-authorization-server` |
| Protected resource metadata | `https://mcp.example.com/.well-known/oauth-protected-resource` (path-scoped: append the MCP request URI) |
| Bearer token (glama only) | `~/.config/mcp-gateway/bearer-token` |

## Server URLs
- glama: `https://mcp.example.com/oauth/mcp` (or bearer `/mcp`)
- any other: `https://mcp.example.com/oauth/ex/<name>/mcp`

### CLI / agent MCP servers (live)

Working set only — same four names as the README inventory / `servers.example.json`.

| Name | URL | Use for |
|------|-----|---------|
| **acpx-cli** | `…/oauth/ex/acpx-cli/mcp` | **Grok** (`exec`/`prompt`), **Qwen** (`exec_qwen`), **OpenCode** (`exec_opencode`); sessions |
| gh-cli | `…/oauth/ex/gh-cli/mcp` | GitHub CLI |
| oci-cli | `…/oauth/ex/oci-cli/mcp` | Oracle Cloud CLI |
| openscad | `…/oauth/ex/openscad/mcp` | OpenSCAD render |

Local (no TLS): `http://127.0.0.1:<port>/mcp` — acpx-cli is **:8826**.

Parked / disabled CLIs (configs kept; **not** in `servers.json` / `servers.example.json`): `vibe-cli`, `agy-cli`, `grok-cli`, `qwen-cli`, `claude-cli`, `codex-cli`, `gemini-cli` — see `configs/DISABLED-cli.md`. Use **acpx-cli** where noted.

### SaaS / OpenAPI servers
`flyio` `render` `slack` `getzep` `virustotal` `notion` `asana` `apisguru` `netbox` `wolframalpha` `github` `netlify` `gpt-terminal-plus` (`box` is manual-only).
**wordpress-dev** is disabled (`enabled: false`; Awardspace live wp-json). **open-swarm-oracle** is discontinued (not in the manifest). See `configs/DISABLED-openapi.md`.
`gpt-terminal-plus` tool annotations come from engine 0.4.0 ([mcp-openapi-proxy#69](https://github.com/matthewhand/mcp-openapi-proxy/issues/69) / [PR #70](https://github.com/matthewhand/mcp-openapi-proxy/pull/70)).

## Wire **acpx-cli** (recommended agent entry)

1. Add MCP server **acpx-cli**  
   **Server URL:** `https://mcp.example.com/oauth/ex/acpx-cli/mcp`  
   **Transport:** Streamable HTTP / HTTP (not stdio).
2. **Auth (required — this gateway is not open):**
   - OAuth **authorization server:** `https://mcp.example.com`  
     (discovery: `/.well-known/oauth-authorization-server`)  
   - **Static confidential client** (always available):
     - `client_id` = `grok`
     - `client_secret` = value in `~/.config/mcp-gateway/oauth.conf`
     - scope = `mcp`
     - PKCE S256 supported
   - **Dynamic Client Registration** (RFC 7591): discovery advertises
     `{issuer}/oauth/register`. Public PKCE clients (`token_endpoint_auth_method: none`)
     can `POST` JSON (`redirect_uris`, `grant_types: ["authorization_code"]`,
     `response_types: ["code"]`) and receive a `client_id` **without** a secret.
     Records persist in `~/.state/mcp-gateway/oauth-clients.json` (or `$MCP_OAUTH_CLIENTS`).
     Authorize/token require PKCE and an exact `redirect_uri` match.
   - **Client ID Metadata Documents** (CIMD): discovery sets
     `client_id_metadata_document_supported: true`. An HTTPS URL `client_id`
     (with a path) is fetched as JSON, validated (`client_id` exact match,
     `client_name`, `redirect_uris` containing the request URI), and cached
     per HTTP cache headers. CIMD clients are public PKCE; they are **not**
     written to the DCR store.
3. After connect, tools: `exec`, `exec_qwen`, `exec_opencode`, `prompt`, `sessions_*`, `status`, `cancel`, `run`, `version`.
4. **Always pass** `working_directory` (absolute path under allowed roots, e.g. `/home/chatgpt/…`).

| Goal | Tool | Args sketch |
|------|------|-------------|
| One-shot Grok | `exec` | `{ "prompt": "…", "working_directory": "/home/chatgpt/proj" }` |
| One-shot Qwen | `exec_qwen` | same |
| One-shot OpenCode | `exec_opencode` | same |
| Multi-turn Grok | `sessions_ensure` then `prompt` | optional `"session": "job1"` on both |

Agent names in the shell: **`grok-build`**, `qwen`, `opencode` (see `configs/acpx-AGENTS.md`).

### If the client says “offline” / cannot connect

| Symptom | Meaning / fix |
|---------|----------------|
| HTTP **401** without token | **Normal** — OAuth not completed. Body must be JSON `{"error":"invalid_token"}` (not nginx HTML). `WWW-Authenticate` names the path-scoped PRM for this URL. Finish login or set Bearer token. |
| HTTP **404** | Wrong path; must be `/oauth/ex/acpx-cli/mcp` |
| OAuth fails / “registration” error | Prefer DCR (`POST /oauth/register`), a CIMD HTTPS URL `client_id`, or set **client_id=`grok`** + **client_secret** from `oauth.conf`. |
| Spark lists tools but cannot **call** | Challenge must include `resource_metadata=…/oauth-protected-resource<request-uri>` so listing vs `tools/call` bind the same path-scoped PRM. See curl check below. |
| Connect works locally only | Use public URL above; local is `http://127.0.0.1:8826/mcp` (no OAuth) |
| Backend check on host | `systemctl is-active mcp-cli-server@acpx-cli` + `curl -sS http://127.0.0.1:8826/mcp` initialize |

Verified on gateway: OAuth code→token→`initialize`→`tools/list` against acpx-cli returns **200** with tools.

### Gemini Spark / 401 challenge (RFC 9728)

Unauthenticated `POST` to an OAuth MCP URL is **discovery**, not a hard protocol cut.
Grok path-secret, glama `/mcp` bearer, DCR, and CIMD stay available.

```bash
# Must be 401 JSON invalid_token (not nginx HTML) with path-scoped PRM.
curl -sS -D - -o /tmp/mcp401.json -X POST \
  https://mcp.example.com/oauth/ex/acpx-cli/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# WWW-Authenticate: Bearer realm="OAuth", resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource/oauth/ex/acpx-cli/mcp", error="invalid_token"
# body: {"error":"invalid_token"}

curl -sS https://mcp.example.com/.well-known/oauth-protected-resource/oauth/ex/acpx-cli/mcp
# {"resource":"https://mcp.example.com/oauth/ex/acpx-cli/mcp", ...}
```

`resource` in that document must be the URL the client dialed (host + path). A root-only PRM
makes Spark list tools against one resource and fail `tools/call` against another.

## Per client

### Grok (connectors)
The existing connector uses the path-secret route `https://mcp.example.com/grok/<token>`
(`~/.config/mcp-gateway/grok-url`). To add others, point a connector at the OAuth URL above with
`client_id=grok` + the secret, or use the bearer route for glama.

To use **coding agents** from Grok, also add the **acpx-cli** OAuth URL above (not the path-secret alone if it only targets glama).

### Claude Desktop / Claude Code
`claude mcp add --transport http acpx-cli https://mcp.example.com/oauth/ex/acpx-cli/mcp`
then approve; Claude runs the OAuth flow. For glama bearer:
`--header "Authorization: Bearer <token>"` against `/mcp`.

### Open WebUI / LibreChat / server-side clients
Add an MCP server with the OAuth URL + `client_id`/`client_secret`. These proxy server-side, so
no CORS concerns.

### Browser / in-page clients
Only `/mcp` (glama bearer) is CORS-enabled. The `/oauth/*` paths need a CORS pass before a pure
browser client can complete OAuth — see ROADMAP (not yet done).

## Two-layer auth reminder
1. **Client → gateway** (the OAuth/bearer above) — gets you connected.
2. **Gateway → upstream API** (each server's own key in `servers.json`, or host CLI auth for acpx/oci/gh) — needed for live tool *calls*.
A connected client whose tool calls return `401` means layer 2 (upstream key) is missing for that server.
