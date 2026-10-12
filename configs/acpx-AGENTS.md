# acpx agents on this host

Runtime: `acpx` 0.12 via **acpx-cli** MCP (`:8826` / bridge `:3870`).  
Wrapper: `bin/acpx-wrap.sh` (PATH + LiteLLM Fly env + best-effort Codex/Gemini auth).  
Registry: `~/.acpx/config.json` (absolute agent commands).

## Defaults

| Setting | Value |
|---------|--------|
| **MCP default coding tool** | **`exec`** → **Qwen** (LiteLLM Fly) |
| **Grok override** | **`exec_grok`** → Grok subscription |
| **acpx `defaultAgent`** | **`qwen`** (bare `acpx exec` also uses Qwen) |

Calling agents that only know a single “run coding agent” tool should call **`exec`**.  
To force Grok, call **`exec_grok`** (or `acpx grok-build exec …` on the host).

## LiteLLM Fly

Coding agents (except Grok) use **https://open-litellm.fly.dev** via ACP helpers under
`~/.litellm/scripts/acp/`.

| Agent | ACP helper | Notes |
|-------|------------|--------|
| **qwen** (default) | `qwen-acp.sh` | MCP `exec` / `exec_qwen` |
| gemini | `gemini-acp.sh` | Do not force `--model code` (ACP only lists Google ids) |
| opencode | `opencode-acp.sh` | model `litellm/code` |
| kilocode | `kilocode-acp.sh` | model `litellm/code` |
| codex | `codex-acp.sh` | LiteLLM via isolated CODEX_HOME |
| **grok-build** | `grok agent stdio` | Subscription only — **`exec_grok`** |

**claude** is disabled (removed from registry).

## Exposed on MCP

| MCP tool | Agent | Purpose |
|----------|--------|---------|
| **`exec`** | **qwen** | **Default one-shot coding** (LiteLLM) |
| **`exec_grok`** | grok-build | **Override** — Grok subscription |
| `exec_qwen` | qwen | Explicit alias of default |
| `exec_opencode` | opencode | LiteLLM `litellm/code` |
| `exec_kilocode` | kilocode | LiteLLM `litellm/code` |
| `exec_gemini` | gemini | LiteLLM env; agent default model |
| `exec_codex` | codex | LiteLLM (auth may still be flaky) |
| `prompt`, `sessions_*`, `status`, `cancel` | grok-build | Multi-turn Grok sessions (unchanged) |
| `config_show`, `version` | — | Diagnostics |

Public URL: `https://mcp.example.com/oauth/ex/acpx-cli/mcp`

## Not ACP — dedicated MCP CLIs

| CLI | MCP unit |
|-----|----------|
| vibe | `vibe-cli` (**parked** 2026-08-17 — not in the live manifest) |
| letta | Letta path |
| agy | `agy-cli` (**parked** 2026-08-17 — not in the live manifest) |
| gh / oci / openscad | their units (live) |

## Client call pattern

**Default (Qwen + LiteLLM):**
```json
{
  "prompt": "fix the failing tests",
  "working_directory": "/home/chatgpt/your-project"
}
```
Tool: **`exec`** (or **`exec_qwen`**).

**Override to Grok:**
```json
{
  "prompt": "fix the failing tests",
  "working_directory": "/home/chatgpt/your-project"
}
```
Tool: **`exec_grok`**.

There is **no** load-balancing across agents: the caller picks the tool. LiteLLM may load-balance **within** a model pool (e.g. `code`) after the request hits Fly.

## Host smoke

```bash
# default coding (qwen)
~/mcp-gateway/bin/acpx-wrap.sh --approve-all --format text qwen exec "Say READY"

# grok override
~/mcp-gateway/bin/acpx-wrap.sh --approve-all --format text grok-build exec "Say READY"
```

## Restart after config changes

```bash
systemctl --user restart mcp-cli-server@acpx-cli
```
