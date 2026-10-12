#!/usr/bin/env python3
"""Unit tests for POST /run (known-prefix + env arbitrary command)."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


bridge = _load("cli_bridge_run", ROOT / "bin" / "cli_bridge.py")


def _gh_cfg() -> Any:
    return bridge.BridgeConfig.load(ROOT / "configs" / "gh.json")


def test_run_auto_injected_on_openapi():
    cfg = _gh_cfg()
    assert cfg.allow_run is True
    spec = bridge.build_openapi(cfg)
    assert "/run" in spec["paths"]
    post = spec["paths"]["/run"]["post"]
    props = post["requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert "args" in props
    assert "working_directory" in props
    assert "command" not in props  # env not set


def test_run_disabled_by_config():
    cfg = _gh_cfg()
    cfg.allow_run = False
    ops = bridge._with_run_operation(cfg, list(cfg.operations))
    assert not any(o.get("path") == "/run" for o in ops)
    spec = bridge.build_openapi(cfg)
    assert "/run" not in spec["paths"]


def test_run_disabled_by_env():
    cfg = _gh_cfg()
    with mock.patch.dict(os.environ, {"CLI_BRIDGE_DISABLE_RUN": "1"}):
        ops = bridge._with_run_operation(cfg, list(cfg.operations))
        assert not any(o.get("type") == "run" for o in ops)


def test_reject_unknown_prefix():
    cfg = _gh_cfg()
    try:
        bridge._execute_run(cfg, {"args": ["secret", "leak"]}, {})
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "known prefixes" in str(e)


def test_allow_version_without_cwd():
    cfg = _gh_cfg()
    result = bridge._execute_run(cfg, {"args": ["--version"]}, {})
    assert result["exit_code"] == 0
    assert result["argv"] == ["/usr/bin/gh", "--version"]
    assert "gh version" in (result["stdout"] + result["stderr"]).lower() or result["stdout"]


def test_repo_view_requires_cwd():
    cfg = _gh_cfg()
    try:
        bridge._execute_run(cfg, {"args": ["repo", "view"]}, {})
        raise AssertionError("expected missing working_directory")
    except ValueError as e:
        assert "working_directory" in str(e)


def test_repo_view_with_cwd():
    cfg = _gh_cfg()
    wd = str(ROOT)
    result = bridge._execute_run(
        cfg,
        {"args": ["repo", "view", "--json", "name"], "working_directory": wd},
        {"working_directory": wd},
    )
    assert result["exit_code"] == 0
    assert result["argv"][:3] == ["/usr/bin/gh", "repo", "view"]
    assert result["working_directory"] == str(Path(wd).resolve())


def test_command_override_blocked_by_default():
    cfg = _gh_cfg()
    try:
        bridge._execute_run(cfg, {"command": "/bin/echo", "args": ["hi"]}, {})
        raise AssertionError("expected block")
    except ValueError as e:
        assert "CLI_BRIDGE_ALLOW_ARBITRARY_COMMAND" in str(e)


def test_command_override_with_env():
    cfg = _gh_cfg()
    with mock.patch.dict(os.environ, {"CLI_BRIDGE_ALLOW_ARBITRARY_COMMAND": "1"}):
        result = bridge._execute_run(cfg, {"command": "/bin/echo", "args": ["hello-run"]}, {})
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "hello-run"
    assert result["argv"] == ["/bin/echo", "hello-run"]


def test_command_override_requires_absolute_path():
    cfg = _gh_cfg()
    with mock.patch.dict(os.environ, {"CLI_BRIDGE_ALLOW_ARBITRARY_COMMAND": "1"}):
        try:
            bridge._execute_run(cfg, {"command": "echo", "args": ["x"]}, {})
            raise AssertionError("expected absolute path error")
        except ValueError as e:
            assert "absolute" in str(e)


def test_openapi_includes_command_when_env_set():
    cfg = _gh_cfg()
    with mock.patch.dict(os.environ, {"CLI_BRIDGE_ALLOW_ARBITRARY_COMMAND": "1"}):
        spec = bridge.build_openapi(cfg)
    props = spec["paths"]["/run"]["post"]["requestBody"]["content"]["application/json"]["schema"][
        "properties"
    ]
    assert "command" in props


def test_run_require_known_prefix_false_allows_any_args():
    cfg = _gh_cfg()
    cfg.run_require_known_prefix = False
    # Still fixed binary; --help is not a defined prefix but should work
    result = bridge._execute_run(cfg, {"args": ["--help"]}, {})
    assert result["argv"][0] == "/usr/bin/gh"
    assert result["exit_code"] in (0, 1)  # gh --help exits 0


def test_execute_operation_dispatch():
    cfg = _gh_cfg()
    op = bridge._default_run_operation(cfg)
    result = bridge.execute_operation(
        cfg, op, [], {}, {"args": ["--version"]}, None
    )
    assert result["exit_code"] == 0


def test_openscad_run_prefix_version():
    cfg = bridge.BridgeConfig.load(ROOT / "configs" / "openscad.json")
    spec = bridge.build_openapi(cfg)
    assert "/run" in spec["paths"]
    result = bridge._execute_run(cfg, {"args": ["--version"]}, {})
    assert result["exit_code"] == 0
    assert "openscad" in (result["stdout"] + result["stderr"]).lower() or result["argv"][-1] == "--version"


def test_example_cli_existing_run_path_not_duplicated():
    """example-cli.json already defines path /run as a normal cli op — do not inject type=run."""
    cfg = bridge.BridgeConfig.load(ROOT / "configs" / "example-cli.json")
    ops = bridge._with_run_operation(cfg, list(cfg.operations))
    run_ops = [o for o in ops if o.get("path") == "/run"]
    assert len(run_ops) == 1
    assert run_ops[0].get("type", "cli") != "run" or run_ops[0].get("id") == "run"
    # Existing example uses type default cli with include_flags — type not "run"
    assert run_ops[0].get("type", "cli") == "cli"


def main() -> int:
    tests = [
        test_run_auto_injected_on_openapi,
        test_run_disabled_by_config,
        test_run_disabled_by_env,
        test_reject_unknown_prefix,
        test_allow_version_without_cwd,
        test_repo_view_requires_cwd,
        test_repo_view_with_cwd,
        test_command_override_blocked_by_default,
        test_command_override_with_env,
        test_command_override_requires_absolute_path,
        test_openapi_includes_command_when_env_set,
        test_run_require_known_prefix_false_allows_any_args,
        test_execute_operation_dispatch,
        test_openscad_run_prefix_version,
        test_example_cli_existing_run_path_not_duplicated,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"OK  {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
