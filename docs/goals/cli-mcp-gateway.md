# Goal: CLI tools as reliable MCP services

**Status:** ACHIEVED (2026-07-09); live CLI set narrowed 2026-08-17  
**Owner:** agent-maintainable — re-verify with checklist below after host/config changes  
**Repo commits:** `b6e45aa` (CLI stack), `45e92fa` (webui builder)

> **Live CLI set now:** `openscad`, `gh-cli`, `oci-cli`, `acpx-cli` only.
> `vibe-cli` / `agy-cli` were parked 2026-08-17 (see `configs/DISABLED-cli.md`).
> Treat the README inventory + example manifest as SoT; this goal doc is historical.

## One-liner

Wrap useful CLIs as MCP tools on mcp-gateway so agents can call them reliably (OpenSCAD images + agents via acpx/vibe/qwen/agy + gh/oci).

## Acceptance criteria (live)

1. **Live set only (2026-08-17+):** `openscad`, `gh-cli`, `oci-cli`, **`acpx-cli`**. Parked: vibe-cli, agy-cli. Disabled: grok-cli, qwen-cli, claude/codex/gemini-cli. Agents via acpx: grok-build, qwen, opencode.
2. **Units:** each runs as `mcp-cli-server@<name>` (active/enabled), not proxy-only `mcp-server@`.
3. **MCP smoke:** for each live CLI, streamable HTTP `initialize` → `tools/list` → `tools/call version` succeeds.
4. **Agent prompts:** acpx-cli `exec` (grok-build) + vibe / qwen / agy `tools/call prompt` with short instruction + `working_directory` returns real CLI stdout.
5. **OpenSCAD images:** unit tests + e2e prove default ImageContent+ResourceLink path and `return_inline=false` URL-only path.
6. **gh-cli:** `version`, `repo_view`, `issue_list`/`pr_list` (cwd repo), `api`, `auth_status` work via MCP (host must have `gh auth login`).
7. **oci-cli:** compute + network (VCN/subnet/security-list/NSG/gateways) + network-firewall + db/mysql/nosql + LB + search + `run` (prefix-limited); host `~/.oci/config` API key (ap-sydney-1).
8. **Disabled agents:** configs may remain on disk; units disabled; see `configs/DISABLED-cli.md`.

## Ports (canonical)

| Server | MCP | Bridge |
|--------|-----|--------|
| openscad | 8816 | 3860 |
| vibe-cli | 8818 | 3862 |
| agy-cli | 8823 | 3867 |
| gh-cli | 8824 | 3868 |
| oci-cli | 8825 | 3869 |
| acpx-cli | 8826 | 3870 |

## Re-verify (agent / operator)

```bash
cd ~/mcp-gateway

# Manifest + units
python3 -c "import json;d=json.load(open('servers.json'));print(sorted(k for k,v in d.items() if 'cli_config' in v))"
systemctl list-units 'mcp-cli-server@*' --no-legend

# Generators / OpenSCAD unit tests
python3 webui/test_generators.py
python3 bin/test_openscad_output_paths.py
python3 bin/test_mcp_content_expand.py

# Live OpenSCAD MCP e2e (needs mcp-cli-server@openscad)
python3 bin/test_openscad_mcp_e2e.py
```

Optional: probe each agent `version` + short `prompt` via `http://127.0.0.1:<port>/mcp`.

## Non-goals (unless a new goal is opened)

- Re-enable / fix claude, codex, gemini
- Remove snapd wholesale
- Commit live `servers.json` (secrets)

## Change log

| Date | Note |
|------|------|
| 2026-07-08 | CLI stack live; OpenSCAD URL/inline proven; agents prompt OK |
| 2026-07-08 | Disabled claude/codex/gemini; commit `b6e45aa` |
| 2026-07-09 | Webui builder committed `45e92fa`; this goal doc added for ongoing maintain |
| 2026-07-09 | **gh-cli** live (:8824/:3868): issue/pr list, repo view, api, auth_status; argv ops gain optional flags |
| 2026-07-09 | **POST /run** default-on: raw args limited to defined op argv prefixes; `CLI_BRIDGE_ALLOW_ARBITRARY_COMMAND=1` for absolute command override; `CLI_BRIDGE_DISABLE_RUN=1` to hide |
| 2026-07-09 | **oci-cli** live (:8825/:3869): IAM/compute/network/os/search tools + prefix-limited `/run`; auth via host `~/.oci/config` |
| 2026-07-09 | **oci-cli** expanded: security lists/NSGs, subnets/gateways, network-firewall, db system/ADB/mysql/nosql, LB/NLB |
| 2026-07-09 | **acpx-cli** live (:8826/:3870): headless ACP client (`acpx` 0.12) default agent grok-build; exec/prompt/sessions + `/run` |
| 2026-07-09 | **grok-cli disabled** — use acpx-cli `exec`/`prompt` (`grok-build`); config kept in `configs/grok.json` |
| 2026-07-09 | **acpx polish:** wrap script + multi-agent exec (grok/qwen/opencode OK; codex/claude/gemini degraded); see `configs/acpx-AGENTS.md` |
| 2026-07-10 | Client polish: hide flaky acpx tools; disable qwen-cli; CLIENTS.md acpx wiring; MySQL VCN-only note |
| 2026-08-17 | **vibe-cli / agy-cli parked** — removed from live manifest / example; use acpx later (`configs/DISABLED-cli.md`) |
