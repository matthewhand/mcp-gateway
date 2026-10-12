#!/usr/bin/env python3
"""Exercise McpGatewayGenerators from gateway-builder.html (no browser required)."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "webui" / "gateway-builder.html"
CLI_BRIDGE = ROOT / "bin" / "cli_bridge.py"

# Representative CLI form state (Grok-like)
REPRESENTATIVE_CLI_STATE = {
    "command": "/home/chatgpt/.local/bin/grok",
    "commandAsArray": False,
    "title": "Grok CLI",
    "version": "0.2.91",
    "parseHelp": True,
    "rejectUnknown": True,
    "sandboxOutputs": False,
    "inlineBinary": True,
    "timeoutSec": 600,
    "helpArgv": "",
    "blacklist": [],
    "wdMode": "request",
    "wdParam": "working_directory",
    "wdMapsFlag": "cwd",
    "wdRoots": ["/home/chatgpt"],
    "wdRequired": True,
    "wdDescription": "Required per call: project directory chosen by the MCP client (passed as --cwd)",
    "promptDescription": "Single-turn prompt (always passed as grok -p / --single)",
    "promptMode": "flag",
    "promptMapsFlag": "single",
    "promptRequired": True,
    "flags": [
        {
            "name": "model",
            "openapiName": "",
            "description": "Model ID (grok -m)",
            "type": "string",
            "enumValues": [],
            "inPrompt": True,
            "fixedValue": "",
        },
        {
            "name": "effort",
            "openapiName": "",
            "description": "",
            "type": "string",
            "enumValues": ["low", "medium", "high", "xhigh", "max"],
            "inPrompt": True,
            "fixedValue": "",
        },
        {
            "name": "output-format",
            "openapiName": "",
            "description": "Headless output format",
            "type": "string",
            "enumValues": ["plain", "json", "streaming-json"],
            "inPrompt": True,
            "fixedValue": "",
        },
        {
            "name": "no-alt-screen",
            "openapiName": "",
            "description": "",
            "type": "boolean",
            "enumValues": [],
            "inPrompt": False,
            "fixedValue": "true",
        },
    ],
    "opId": "prompt",
    "opPath": "/prompt",
    "opSummary": "Run a single-turn Grok prompt",
    "opDescription": "Mandatory --single (-p) is driven by the prompt body field.",
    "versionArgv": '["--version"]',
}


def extract_generators_script() -> str:
    text = HTML.read_text()
    match = re.search(r"<!-- GENERATORS.*?-->\s*<script>(.*?)</script>", text, re.DOTALL)
    if not match:
        raise RuntimeError("generator script block not found in gateway-builder.html")
    return match.group(1)


def run_js(fn: str, payload: dict) -> dict:
    script = extract_generators_script()
    runner = (
        "global.window = global;\n"
        + script
        + """
const G = global.McpGatewayGenerators;
const payload = JSON.parse(process.argv[2] || "{}");
const fn = process.argv[3];
let out;
if (fn === "cli") out = G.generateCliConfig(payload);
else if (fn === "endpoint") out = G.generateEndpointArtifacts(payload);
else if (fn === "servers") out = G.generateCliServersEntry(payload);
else if (fn === "samples") out = G.CLI_SAMPLES;
else if (fn === "eval") out = { ok: true, keys: Object.keys(G) };
else throw new Error("unknown fn: " + fn);
console.log(JSON.stringify(out));
"""
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(runner)
        path = fh.name
    try:
        proc = subprocess.run(
            ["node", path, json.dumps(payload), fn],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout)
        return json.loads(proc.stdout)
    finally:
        Path(path).unlink(missing_ok=True)


def test_script_eval() -> None:
    result = run_js("eval", {})
    assert result["ok"] is True
    expected = {
        "generateCliConfig",
        "generateCliConfigJson",
        "generateCliServersEntry",
        "generateNginxLocation",
        "generateEndpointArtifacts",
        "parseCliConfig",
        "parseEndpointEntry",
        "CLI_SAMPLES",
        "ENDPOINT_SAMPLES",
    }
    assert expected.issubset(set(result["keys"]))


def test_generators_load() -> None:
    samples = run_js("samples", {})
    for key in ("grok", "vibe", "qwen", "agy", "openscad"):
        assert key in samples, f"missing live sample {key}"
    assert samples["grok"]["title"] == "Grok CLI"
    assert samples["qwen"]["parseHelp"] is False
    assert samples["agy"]["promptMapsFlag"] == "print"


def test_cli_generation_full_state() -> dict:
    cfg = run_js("cli", REPRESENTATIVE_CLI_STATE)
    assert cfg["command"] == "/home/chatgpt/.local/bin/grok"
    assert cfg["working_directory"]["maps_to_flag"] == "cwd"
    assert cfg["working_directory"]["required"] is True
    assert cfg["working_directory"]["allowed_roots"] == ["/home/chatgpt"]
    assert cfg["body_params"]["prompt"]["required"] is True
    assert cfg["body_params"]["prompt"]["maps_to_flag"] == "single"
    assert "model" in cfg["whitelist"]
    assert cfg["overrides"]["effort"]["enum"] == ["low", "medium", "high", "xhigh", "max"]
    assert cfg["operations"][0]["fixed_flags"]["no-alt-screen"] is True
    assert "model" in cfg["operations"][0]["include_flags"]
    return cfg


def test_required_defaults_when_omitted() -> dict:
    state = dict(REPRESENTATIVE_CLI_STATE)
    del state["promptRequired"]
    del state["wdRequired"]
    cfg = run_js("cli", state)
    assert cfg["body_params"]["prompt"]["required"] is True
    assert cfg["working_directory"]["required"] is True
    return cfg


def test_grok_sample_drop_in() -> dict:
    grok_sample = run_js("samples", {})["grok"]
    cfg = run_js("cli", grok_sample)
    assert cfg["body_params"]["prompt"]["required"] is True
    assert cfg["working_directory"]["required"] is True
    assert cfg["working_directory"]["maps_to_flag"] == "cwd"
    return cfg


def test_openscad_servers_entry() -> None:
    samples = run_js("samples", {})
    entry = run_js("servers", samples["openscad"])
    block = entry["openscad"]
    assert block["port"] == 8816
    assert block["cli_port"] == 3860
    assert block["env"]["TOOL_WHITELIST"] == "/render,/files,/version,/run"
    assert block["env"]["TOOL_NAME_USE_OPERATION_ID"] == "true"
    assert block["env"]["CAPABILITIES_RESOURCES"] == "true"
    assert "SERVER_URL_OVERRIDE" in block["env"]


def test_agent_servers_entry() -> None:
    samples = run_js("samples", {})
    entry = run_js("servers", samples["qwen"])
    block = entry["qwen-cli"]
    assert block["env"]["TOOL_WHITELIST"] == "/prompt,/version,/run"
    assert "TOOL_NAME_USE_OPERATION_ID" not in block["env"]


def test_openscad_config_shape() -> None:
    samples = run_js("samples", {})
    cfg = run_js("cli", samples["openscad"])
    assert cfg["sandbox_files"]["public_slug"] == "openscad"
    assert "previews" in cfg["sandbox_files"]["resource_dirs"]
    paths = {op["path"] for op in cfg["operations"]}
    assert "/render/png" in paths
    assert "/files/{filepath}" in paths
    dl = next(op for op in cfg["operations"] if op["id"] == "download_file")
    assert dl.get("path_params") == ["filepath"]


def test_endpoint_generation() -> dict:
    state = {
        "name": "gpt-terminal-plus",
        "port": 8815,
        "specUrl": "http://localhost:3100/openapi.json",
        "toolWhitelist": "/command/execute-shell,/file/read",
        "serverUrlOverride": "http://localhost:3100",
        "apiKey": "${TERMINAL_API_KEY}",
        "apiAuthType": "Bearer",
        "openApiSpecFormat": "",
        "extraEnv": {"STRIP_PARAM": "token"},
    }
    art = run_js("endpoint", state)
    entry = art["serversEntry"]["gpt-terminal-plus"]
    assert entry["port"] == 8815
    assert entry["env"]["TOOL_WHITELIST"] == "/command/execute-shell,/file/read"
    assert entry["env"]["STRIP_PARAM"] == "token"
    assert "/oauth/ex/gpt-terminal-plus/mcp" in art["nginxBlock"]
    assert "proxy_pass http://127.0.0.1:8815" in art["nginxBlock"]
    assert "rewrite ^/oauth/ex/gpt-terminal-plus/mcp" in art["nginxBlock"]
    assert "error_page 401 = @mcp_oauth_401" in art["nginxBlock"]
    return art


def test_cli_bridge_generate(cfg: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="mcp-webui-test-") as tmp:
        tmp_path = Path(tmp)
        cfg_path = tmp_path / "test-generated-cli.json"
        cfg_path.write_text(json.dumps(cfg, indent=2))
        proc = subprocess.run(
            [sys.executable, str(CLI_BRIDGE), "generate", "--config", str(cfg_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"cli_bridge generate failed:\n{proc.stderr}\n{proc.stdout}")
        spec = json.loads(proc.stdout)
        paths = spec.get("paths", {})
        assert "/prompt" in paths
        assert "/version" in paths
        prompt_post = paths["/prompt"]["post"]
        schema = (
            prompt_post.get("requestBody", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema", {})
        )
        body_props = schema.get("properties", {})
        required = schema.get("required") or []
        assert "prompt" in body_props
        assert "working_directory" in body_props
        assert "prompt" in required
        assert "working_directory" in required
        for flag in cfg["operations"][0]["include_flags"]:
            assert flag in cfg["whitelist"]
        return spec


def main() -> int:
    if not HTML.is_file():
        print(f"MISSING {HTML}", file=sys.stderr)
        return 1

    test_script_eval()
    print("ok: script eval")

    test_generators_load()
    print("ok: generators load (live samples present)")

    cfg = test_cli_generation_full_state()
    print("ok: cli generation (full state)")

    test_required_defaults_when_omitted()
    print("ok: required defaults when omitted")

    test_grok_sample_drop_in()
    print("ok: grok sample drop-in (required fields)")

    test_openscad_servers_entry()
    print("ok: openscad servers entry env")

    test_agent_servers_entry()
    print("ok: agent servers entry env")

    test_openscad_config_shape()
    print("ok: openscad config shape")

    art = test_endpoint_generation()
    print("ok: endpoint generation")
    _ = art

    test_cli_bridge_generate(cfg)
    print("ok: cli_bridge generate")

    print("all webui generator tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
