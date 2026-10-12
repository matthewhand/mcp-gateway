# Disabled CLI servers

Configs retained for re-enable; units stopped and removed from `servers.json` / `servers.example.json`.

| Name | Config | Was port / cli_port | Status |
|------|--------|---------------------|--------|
| gemini-cli | `configs/gemini.json` | 8819 / 3863 | disabled 2026-07-08 — prompt 500 |
| codex-cli | `configs/codex.json` | 8820 / 3864 | disabled 2026-07-08 — prompt 500 |
| claude-cli | `configs/claude.json` | 8821 / 3865 | disabled 2026-07-08 — prompt 500 |
| **grok-cli** | `configs/grok.json` | 8817 / 3861 | **disabled 2026-07-09** — use **acpx-cli** `exec` / `prompt` (`grok-build`) |
| **qwen-cli** | `configs/qwen.json` | 8822 / 3866 | **disabled 2026-07-10** — use **acpx-cli** `exec_qwen` |
| **vibe-cli** | `configs/vibe.json` | 8818 / 3862 | **disabled 2026-08-17** — park; use acpx later |
| **agy-cli** | `configs/agy.json` | 8823 / 3867 | **disabled 2026-08-17** — park; use acpx later |

Working CLI set: **acpx-cli**, **gh-cli**, **oci-cli**, **openscad**.

## Agents via acpx-cli (preferred)

Public: `https://mcp.example.com/oauth/ex/acpx-cli/mcp`  
Local: `http://127.0.0.1:8826/mcp`  
Matrix: `configs/acpx-AGENTS.md`

| MCP tool | Agent |
|----------|--------|
| `exec`, `prompt`, sessions/* | grok-build |
| `exec_qwen` | qwen |
| `exec_opencode` | opencode |

## Re-enable a disabled unit

```bash
# restore servers.json entry + nginx /oauth/ex/<name>/mcp from git history
sudo systemctl enable --now mcp-cli-server@<name>
```
