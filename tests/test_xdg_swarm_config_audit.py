#!/usr/bin/env python3
"""Audit: every live ~/.config/swarm file is used, loadable, and not a stale shell.

Drives real open-swarm loaders (paths, AppConfig load path, library/teams stores,
responses store) against the operator XDG layout used by open-swarm-oracle.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from live_host import is_live_host

HOME = Path.home()
XDG_CFG = HOME / ".config" / "swarm"
XDG_SHARE = HOME / ".local" / "share" / "swarm"
OPEN_SWARM = Path(os.environ.get("OPEN_SWARM_ROOT", str(HOME / "open-swarm"))).resolve()
VENV_PY = OPEN_SWARM / ".venv" / "bin" / "python"

# State-dependent tests below audit the live gateway host's XDG layout
# (~/.config/swarm, the oracle-era swarm_config.json sections, the
# unit-injected .env). On a dev box that layout does not exist, so they skip
# unless we are on the live host (see live_host.py). Hermetic tests
# (dotenv precedence, absence checks) stay ungated.
LIVE_HOST = is_live_host(Path(__file__).resolve().parents[1])
LIVE_HOST_REASON = "requires live gateway host (MCP_LIVE_HOST=1 or servers.json at repo root)"
LIVE_STATE = pytest.mark.skipif(not LIVE_HOST, reason=LIVE_HOST_REASON)

# Files allowed to remain in the live config dir after the audit.
# class: used | unused-with-rationale
LIVE_ALLOWED = {
    "swarm_config.json": "used",
    ".env": "used",  # unit EnvironmentFile + load_swarm_dotenv primary secrets
    "blueprint_library.json": "used",
    "teams.json": "used",
    ".env.pre-masterkey.bak": "deliberate pre-masterkey secrets backup kept by operator (2026-07-14 rotation)",
}

REQUIRED_SWARM_SECTIONS = (
    "llm",
    "mcpServers",
    "filesystem",
    "cli_agents",
    "cli_fusion",
    "cli_orchestrator",
    "cli_map",
    "blueprints",
)


def _open_swarm_env() -> dict[str, str]:
    env = os.environ.copy()
    env["DJANGO_SETTINGS_MODULE"] = "swarm.settings"
    env.setdefault("DJANGO_DEBUG", "true")
    env.setdefault("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost")
    env["SWARM_CONFIG_PATH"] = str(XDG_CFG / "swarm_config.json")
    env["SWARM_RESPONSES_DIR"] = str(XDG_SHARE / "responses")
    # Prefer unit token if present; tests that hit HTTP need a real one.
    env.setdefault("API_AUTH_TOKEN", env.get("API_AUTH_TOKEN", "test-token-not-for-http"))
    return env


def _run_open_swarm(script: str, timeout: int = 60) -> str:
    """Execute a small Python snippet with the open-swarm venv + real packages."""
    if not VENV_PY.is_file():
        pytest.skip(f"open-swarm venv missing: {VENV_PY}")
    proc = subprocess.run(
        [str(VENV_PY), "-c", script],
        cwd=str(OPEN_SWARM),
        env=_open_swarm_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"open-swarm snippet failed ({proc.returncode}):\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc.stdout


@LIVE_STATE
def test_live_config_dir_only_accounted_files():
    """No unaccounted or backup shells remain under ~/.config/swarm/."""
    assert XDG_CFG.is_dir(), f"missing {XDG_CFG}"
    found = {p.name for p in XDG_CFG.iterdir() if p.is_file()}
    # bak leftovers are never allowed — except the allow-listed deliberate backup
    baks = {n for n in found if ".bak" in n or n.endswith(".bak")} - set(LIVE_ALLOWED)
    assert not baks, f"stale backups still in live config dir: {baks}"
    unknown = found - set(LIVE_ALLOWED)
    assert not unknown, f"unaccounted live config files: {unknown}"
    missing = set(LIVE_ALLOWED) - found
    assert not missing, f"expected live files missing: {missing}"


@LIVE_STATE
def test_swarm_config_loaded_by_real_appconfig():
    """SwarmConfig._load_swarm_config must read SWARM_CONFIG_PATH and substitute env."""
    out = _run_open_swarm(
        r"""
import json, os, re
import django
django.setup()
from django.apps import apps
from pathlib import Path

path = Path(os.environ["SWARM_CONFIG_PATH"])
assert path.is_file(), path
sc = apps.get_app_config("swarm")
cfg = sc.config
assert isinstance(cfg, dict) and cfg, "empty app config"
for sec in %r:
    assert sec in cfg, f"missing section {sec}"
# env substitution applied (default model should not remain as ${...} if repo .env loaded)
model = (cfg.get("llm") or {}).get("default", {}).get("model", "")
print(json.dumps({
    "path": str(path),
    "sections": sorted(cfg.keys()),
    "llm_default_model": model,
    "mcp_n": len(cfg.get("mcpServers") or {}),
    "cli_agents": sorted((cfg.get("cli_agents") or {}).keys()),
}))
"""
        % (list(REQUIRED_SWARM_SECTIONS),)
    )
    data = json.loads(out.strip().splitlines()[-1])
    assert data["path"] == str(XDG_CFG / "swarm_config.json")
    assert data["mcp_n"] >= 1
    assert "qwen" in data["cli_agents"] or data["cli_agents"]
    # model should resolve if dotenv from repo works
    assert not str(data["llm_default_model"]).startswith("${"), data


@LIVE_STATE
def test_library_and_teams_loaders_use_xdg_config_dir():
    """blueprint_library.json and teams.json are read via real path helpers."""
    out = _run_open_swarm(
        r"""
import json, os
import django
django.setup()
from pathlib import Path
from swarm.core.paths import get_user_config_dir_for_swarm
from swarm.views.blueprint_library_views import get_user_blueprint_library
from swarm.views.utils import load_dynamic_registry, _dynamic_registry_path

cfg_dir = get_user_config_dir_for_swarm()
assert cfg_dir == Path(os.path.expanduser("~/.config/swarm")) or cfg_dir == Path.home() / ".config" / "swarm"
lib_path = cfg_dir / "blueprint_library.json"
teams_path = _dynamic_registry_path()
assert lib_path.is_file(), lib_path
assert teams_path.is_file(), teams_path
lib = get_user_blueprint_library()
assert "installed" in lib and "custom" in lib
assert isinstance(lib["installed"], list)
assert isinstance(lib["custom"], list)
reg = load_dynamic_registry()
assert isinstance(reg, dict)
print(json.dumps({
    "cfg_dir": str(cfg_dir),
    "lib_path": str(lib_path),
    "teams_path": str(teams_path),
    "installed_n": len(lib["installed"]),
    "custom_n": len(lib["custom"]),
    "teams_n": len(reg),
}))
"""
    )
    data = json.loads(out.strip().splitlines()[-1])
    assert data["lib_path"].endswith("blueprint_library.json")
    assert data["teams_path"].endswith("teams.json")


@LIVE_STATE
def test_responses_store_uses_unit_share_dir():
    """SWARM_RESPONSES_DIR must resolve to ~/.local/share/swarm/responses."""
    out = _run_open_swarm(
        r"""
import json, os
from pathlib import Path
from swarm.core.responses_store import _store_dir, list_summaries
d = _store_dir()
assert d == Path(os.environ["SWARM_RESPONSES_DIR"])
assert d.is_dir()
n = len(list(d.glob("resp_*.json")))
summaries = list_summaries(limit=3)
print(json.dumps({"dir": str(d), "resp_n": n, "summaries_n": len(summaries)}))
"""
    )
    data = json.loads(out.strip().splitlines()[-1])
    assert data["dir"] == str(XDG_SHARE / "responses")
    assert data["resp_n"] >= 0


def test_no_loader_for_archived_sidecar_names():
    """Archived shells must not be required by path helpers for normal boot."""
    # These must not reappear as required files for loaders we exercise.
    for name in ("blueprints.json", "llms.json", "config.json"):
        p = XDG_CFG / name
        assert not p.exists(), f"stale sidecar returned to live dir: {p}"


def test_v1_models_nonempty_when_oracle_up():
    """Live functional path: authenticated GET /v1/models returns models."""
    token = None
    # Unit may inject API_AUTH_TOKEN via EnvironmentFile (not listed in Environment=).
    try:
        show = subprocess.run(
            ["systemctl", "--user", "show", "open-swarm-oracle", "-p", "MainPID", "--value"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        mainpid = (show.stdout or "").strip()
        if mainpid and mainpid != "0":
            env_blob = Path(f"/proc/{mainpid}/environ").read_bytes().split(b"\0")
            for raw in env_blob:
                if raw.startswith(b"API_AUTH_TOKEN="):
                    token = raw.decode().split("=", 1)[1]
                    break
    except Exception:
        token = None
    if not token:
        # Fallback: XDG secrets file (same source as EnvironmentFile=)
        env_file = XDG_CFG / ".env"
        if env_file.is_file():
            for line in env_file.read_text().splitlines():
                if line.startswith("API_AUTH_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not token:
        pytest.skip("open-swarm-oracle unit token not available")

    active = subprocess.run(
        ["systemctl", "--user", "is-active", "open-swarm-oracle"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if active.stdout.strip() != "active":
        pytest.skip("open-swarm-oracle not active")

    req = urllib.request.Request(
        "http://127.0.0.1:8001/v1/models",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
            status = resp.status
    except urllib.error.URLError as e:
        pytest.skip(f"oracle not reachable: {e}")

    assert status == 200
    data = json.loads(body)
    models = data.get("data") or []
    assert len(models) > 0, f"empty models list: {body[:300]}"
    assert all(isinstance(m, dict) and m.get("id") for m in models[:3])


@LIVE_STATE
def test_swarm_config_structural_placeholders_documented():
    """Raw swarm_config placeholders resolve via load_swarm_dotenv (XDG primary)."""
    raw = (XDG_CFG / "swarm_config.json").read_text()
    placeholders = sorted(set(re.findall(r"\$\{([A-Z0-9_]+)\}", raw)))
    assert "LITELLM_MODEL" in placeholders or "OPENAI_API_KEY" in placeholders
    out = _run_open_swarm(
        r"""
import json, os, re
from pathlib import Path
from swarm.utils.dotenv_load import load_swarm_dotenv
from swarm.core import config_loader
load_swarm_dotenv(project_root=Path(%r))
raw = Path(os.environ["SWARM_CONFIG_PATH"]).read_text()
cfg = config_loader._substitute_env_vars(json.loads(raw))
unresolved = sorted(set(re.findall(r"\$\{([A-Z0-9_]+)\}", json.dumps(cfg))))
rue = (cfg.get("blueprints") or {}).get("RueCodeBlueprint") or {}
print(json.dumps({
    "unresolved": unresolved,
    "default_model": cfg["llm"]["default"]["model"],
    "rue_profile": rue.get("llm_profile"),
    "mcp_n": len(cfg.get("mcpServers") or {}),
}))
"""
        % (str(OPEN_SWARM),)
    )
    data = json.loads(out.strip().splitlines()[-1])
    assert not str(data["default_model"]).startswith("${")
    for core in ("LITELLM_MODEL", "LITELLM_BASE_URL", "LITELLM_API_KEY"):
        assert core not in data["unresolved"]
    # pruned dead MCP + fixed profile
    assert data["rue_profile"] == "default"
    assert data["mcp_n"] >= 1
    for dead in ("GETZEP_API_KEY", "HASS_API_KEY", "HASS_URL", "RENDER_API_KEY", "SLACK_API_KEY"):
        assert dead not in data["unresolved"]


def test_load_swarm_dotenv_prefers_xdg_over_project(tmp_path, monkeypatch):
    """Unit/shell env wins; XDG fills next; project is last-resort fallback."""
    if not VENV_PY.is_file():
        pytest.skip("open-swarm venv missing")
    script = r"""
import os, tempfile
from pathlib import Path
from swarm.utils.dotenv_load import load_swarm_dotenv, xdg_swarm_env_path

# isolate HOME
home = Path(os.environ["HOME"])
xdg = home / ".config" / "swarm"
xdg.mkdir(parents=True, exist_ok=True)
(xdg / ".env").write_text("FROM_XDG=1\nSHARED=xdg\n")
proj = Path(os.environ["PROJECT_TMP"])
(proj / ".env").write_text("FROM_PROJECT=1\nSHARED=project\n")
# pre-set unit-like key
os.environ["UNIT_PIN"] = "unit"
# clear targets
for k in ("FROM_XDG", "FROM_PROJECT", "SHARED"):
    os.environ.pop(k, None)
loaded = load_swarm_dotenv(project_root=proj)
assert os.environ.get("UNIT_PIN") == "unit"
assert os.environ.get("FROM_XDG") == "1"
assert os.environ.get("FROM_PROJECT") == "1"
assert os.environ.get("SHARED") == "xdg", os.environ.get("SHARED")
print("ok", ",".join(loaded))
"""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        env = _open_swarm_env()
        env["HOME"] = td
        env["PROJECT_TMP"] = str(tmp_path)
        (tmp_path / ".env").write_text("FROM_PROJECT=1\nSHARED=project\n")
        # also need xdg under td - script creates it
        proc = subprocess.run(
            [str(VENV_PY), "-c", script],
            cwd=str(OPEN_SWARM),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "ok" in proc.stdout


if __name__ == "__main__":
    # Allow `python tests/test_xdg_swarm_config_audit.py` without pytest
    import traceback

    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:
                failed += 1
                print(f"FAIL {name}: {e}")
                traceback.print_exc()
    sys.exit(1 if failed else 0)
