#!/usr/bin/env python3
"""Generic CLI → OpenAPI → HTTP bridge.

Parses ``command --help``, applies whitelist/blacklist and per-flag schema overrides,
emits an OpenAPI 3 spec, and serves HTTP endpoints that execute the CLI.

Whitelist/blacklist control which CLI flags are exposed as OpenAPI parameters AND which
are accepted at execution time. Prefer an explicit whitelist of the options you want
(e.g. rendering flags only); everything else stays hidden. Optional blacklist adds
defense-in-depth for known-dangerous flags (e.g. openscad -m runs shell commands).

Working directory modes (``working_directory`` in config):

  fixed   — server-side only (OpenSCAD). Path is hardwired in config; callers cannot
            override it and the parameter is omitted from OpenAPI.
  request — client-specified every call (Grok). Callers must pass ``working_directory``
            (validated against ``allowed_roots``); optionally mapped to a CLI flag.

Raw run endpoint (``POST /run``, enabled by default via ``allow_run``):

  Body ``{"args": ["issue", "list", "--limit", "5"], "working_directory": "..."}``
  runs ``command + args`` for this bridge only (cannot change the binary).

  Set env ``CLI_BRIDGE_ALLOW_ARBITRARY_COMMAND=1`` to also accept body ``command``
  as an absolute path (any tool). Without that env, ``command`` overrides are rejected.

Usage:
  cli_bridge.py generate --config configs/openscad.json
  cli_bridge.py serve --config configs/openscad.json --port 3860
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

# Max tokens accepted on POST /run (args array length).
_RUN_MAX_ARGS = 200


FLAG_LINE = re.compile(
    r"^\s+"
    r"(?:"
    r"-(?P<short>[A-Za-z])(?:\s+\[\s*--(?P<short_long>[a-zA-Z][\w-]*)\s*\])?"
    r"|--(?P<long>[a-zA-Z][\w-]*)"
    r")"
    r"(?:\s+(?P<has_arg>arg\b.*?))?"
    r"\s{2,}(?P<desc>.*)$"
)


@dataclass
class CliFlag:
    names: list[str]
    takes_value: bool
    description: str = ""

    @property
    def primary(self) -> str:
        for n in self.names:
            if n.startswith("--"):
                return n[2:]
        return self.names[0].lstrip("-")


@dataclass
class BridgeConfig:
    command: list[str]
    title: str
    version: str = "1.0"
    parse_help: bool = True
    help_argv: list[str] = field(default_factory=lambda: ["--help"])
    whitelist: list[str] | None = None
    blacklist: list[str] = None  # type: ignore[assignment]
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    body_params: dict[str, dict[str, Any]] = field(default_factory=dict)
    operations: list[dict[str, Any]] = field(default_factory=list)
    timeout_sec: int = 300
    work_dir: str | None = None
    working_directory: dict[str, Any] = field(default_factory=dict)
    reject_unknown_params: bool = True
    sandbox_outputs: bool = True
    sandbox_files: dict[str, Any] = field(default_factory=dict)
    inline_binary_outputs: bool = True
    # POST /run: raw args under this config's command (default on).
    allow_run: bool = True
    # When true (default), /run args must start with a defined op argv prefix if any exist.
    run_require_known_prefix: bool = True

    @classmethod
    def load(cls, path: str | Path) -> "BridgeConfig":
        raw = json.loads(Path(path).read_text())
        cmd = raw["command"]
        if isinstance(cmd, str):
            cmd = [cmd]
        wd = raw.get("working_directory", {})
        legacy_work_dir = raw.get("work_dir")
        if not wd and legacy_work_dir:
            wd = {"mode": "fixed", "path": legacy_work_dir, "mkdir": True}
        return cls(
            command=cmd,
            title=raw.get("title", Path(cmd[0]).name),
            version=raw.get("version", "1.0"),
            parse_help=raw.get("parse_help", True),
            help_argv=raw.get("help_argv", ["--help"]),
            whitelist=raw.get("whitelist"),
            blacklist=raw.get("blacklist", []),
            overrides=raw.get("overrides", {}),
            body_params=raw.get("body_params", {}),
            operations=raw.get("operations", []),
            timeout_sec=raw.get("timeout_sec", 300),
            work_dir=legacy_work_dir,
            working_directory=wd,
            reject_unknown_params=raw.get("reject_unknown_params", True),
            sandbox_outputs=raw.get("sandbox_outputs", True),
            sandbox_files=raw.get("sandbox_files", {}),
            inline_binary_outputs=raw.get("inline_binary_outputs", True),
            allow_run=raw.get("allow_run", True),
            run_require_known_prefix=raw.get("run_require_known_prefix", True),
        )


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def _arbitrary_command_allowed() -> bool:
    """Env override: allow body.command as absolute path on POST /run."""
    return _env_truthy("CLI_BRIDGE_ALLOW_ARBITRARY_COMMAND")


def _run_disabled_by_env() -> bool:
    return _env_truthy("CLI_BRIDGE_DISABLE_RUN")


def _defined_argv_ops(cfg: BridgeConfig) -> list[tuple[list[str], dict[str, Any]]]:
    """(argv prefix, op) pairs from configured operations (the 'existing tools')."""
    out: list[tuple[list[str], dict[str, Any]]] = []
    for op in cfg.operations or []:
        if op.get("type") in ("run", "upload_file", "download_file", "delete_file", "list_files"):
            continue
        if op.get("argv"):
            out.append(([str(x) for x in op["argv"]], op))
        elif op.get("argv_template"):
            out.append(([str(x) for x in op["argv_template"]], op))
    # Longest prefix first so "issue list" wins over a hypothetical "issue"
    out.sort(key=lambda t: len(t[0]), reverse=True)
    return out


def _defined_argv_prefixes(cfg: BridgeConfig) -> list[list[str]]:
    """Argv prefixes from configured operations (the 'existing tools')."""
    return [p for p, _ in _defined_argv_ops(cfg)]


def _args_match_prefix(args: list[str], prefix: list[str]) -> bool:
    if len(args) < len(prefix):
        return False
    return args[: len(prefix)] == prefix


def _match_run_prefix_op(cfg: BridgeConfig, args: list[str]) -> dict[str, Any] | None:
    for prefix, op in _defined_argv_ops(cfg):
        if _args_match_prefix(args, prefix):
            return op
    return None


def _default_run_operation(cfg: BridgeConfig) -> dict[str, Any]:
    # WD is optional on the schema; execute time uses the matched tool's requires_working_directory.
    return {
        "id": "run",
        "type": "run",
        "path": "/run",
        "method": "post",
        "summary": f"Run {cfg.title} with raw args (restricted to this CLI)",
        "description": (
            "Append raw argv tokens after the configured command. "
            "By default args must start with a defined tool prefix from this config's operations "
            "(when any argv-based tools exist). "
            "Set CLI_BRIDGE_ALLOW_ARBITRARY_COMMAND=1 to pass an absolute command path override."
        ),
        "requires_working_directory": False,
    }


def _with_run_operation(cfg: BridgeConfig, ops: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ops = list(ops)
    if not cfg.allow_run or _run_disabled_by_env():
        return ops
    if any(op.get("id") == "run" or op.get("path") == "/run" or op.get("type") == "run" for op in ops):
        return ops
    ops.append(_default_run_operation(cfg))
    return ops


def _execute_run(
    cfg: BridgeConfig,
    body: dict[str, Any] | None,
    params: dict[str, Any],
) -> dict[str, Any]:
    """POST /run handler: raw args under configured (or env-unlocked) command."""
    body = body or {}
    raw_args = body.get("args", body.get("argv"))
    if raw_args is None:
        raise ValueError("missing required body field: args (array of strings)")
    if not isinstance(raw_args, list) or not all(isinstance(a, str) for a in raw_args):
        raise ValueError("args must be an array of strings")
    if len(raw_args) > _RUN_MAX_ARGS:
        raise ValueError(f"args exceeds max length ({_RUN_MAX_ARGS})")
    # Never pass through a shell. Reject NUL-smuggled tokens.
    for a in raw_args:
        if "\x00" in a:
            raise ValueError("args must not contain NUL")

    matched_op: dict[str, Any] | None = None
    if body.get("command") not in (None, "", []):
        if not _arbitrary_command_allowed():
            raise ValueError(
                "command override requires CLI_BRIDGE_ALLOW_ARBITRARY_COMMAND=1 "
                "(default run is limited to this bridge's configured command)"
            )
        cmd = body["command"]
        if isinstance(cmd, str):
            cmd_list = [cmd]
        elif isinstance(cmd, list) and cmd and all(isinstance(c, str) for c in cmd):
            cmd_list = list(cmd)
        else:
            raise ValueError("command must be a string or array of strings")
        if not Path(cmd_list[0]).is_absolute():
            raise ValueError("command[0] must be an absolute path when overriding")
        if not Path(cmd_list[0]).exists():
            raise ValueError(f"command not found: {cmd_list[0]}")
        argv = cmd_list + list(raw_args)
        # Arbitrary override: cwd optional even if config defaults to required.
        wd_required = False
    else:
        # Restricted: fixed binary; optionally must match a defined tool argv prefix.
        if cfg.run_require_known_prefix:
            prefixes = _defined_argv_prefixes(cfg)
            if prefixes and not any(_args_match_prefix(list(raw_args), p) for p in prefixes):
                known = [json.dumps(p) for p in prefixes]
                raise ValueError(
                    "args must start with a defined tool prefix from this config's operations; "
                    f"known prefixes: {known}"
                )
        matched_op = _match_run_prefix_op(cfg, list(raw_args))
        argv = list(cfg.command) + list(raw_args)
        if matched_op is not None:
            wd_required = _op_requires_working_directory(cfg, matched_op)
        elif cfg.run_require_known_prefix and _defined_argv_prefixes(cfg):
            # Unreachable when prefix check is on (would have raised); keep strict.
            wd = _working_directory_spec(cfg)
            wd_required = wd.get("mode") == "request" and bool(wd.get("required", False))
        else:
            # Free-form args under fixed command (no prefix tools, or prefix check off).
            wd_required = False

    _assert_safe_argv(argv, cfg, params)
    run_cwd = _resolve_run_cwd(cfg, params, required=wd_required)

    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=cfg.timeout_sec,
        cwd=run_cwd,
    )
    return {
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "exit_code": proc.returncode,
        "argv": argv,
        "working_directory": run_cwd,
    }


def _flag_keys(flag: CliFlag) -> set[str]:
    keys: set[str] = set()
    for n in flag.names:
        if n.startswith("--"):
            keys.add(n[2:])
        else:
            keys.add(n.lstrip("-"))
    return keys


def parse_help_text(text: str) -> list[CliFlag]:
    flags: list[CliFlag] = []
    for line in text.splitlines():
        m = FLAG_LINE.match(line)
        if not m:
            continue
        names: list[str] = []
        if m.group("short"):
            names.append(f"-{m.group('short')}")
            if m.group("short_long"):
                names.append(f"--{m.group('short_long')}")
        elif m.group("long"):
            names.append(f"--{m.group('long')}")
        if not names:
            continue
        flags.append(
            CliFlag(
                names=names,
                takes_value=bool(m.group("has_arg")),
                description=(m.group("desc") or "").strip(),
            )
        )
    return flags


def _flags_from_whitelist(cfg: BridgeConfig) -> list[CliFlag]:
    """Synthesize flags from whitelist + overrides when help text is unavailable.

    Needed for CLIs that hang or refuse ``--help`` (gemini/vibe/qwen) and for
    configs with ``parse_help: false`` (agy) so include_flags still resolve.
    """
    keys: list[str] = []
    seen: set[str] = set()
    for source in (cfg.whitelist or [], list(cfg.overrides.keys())):
        for raw in source:
            key = _normalize_key(str(raw))
            if not key or key in seen:
                continue
            seen.add(key)
            keys.append(key)
    # body params that map onto CLI flags must exist for argv building
    for meta in cfg.body_params.values():
        mapped = meta.get("maps_to_flag")
        if mapped:
            key = _normalize_key(str(mapped))
            if key and key not in seen:
                seen.add(key)
                keys.append(key)
    flags: list[CliFlag] = []
    for key in keys:
        ov = cfg.overrides.get(key, {})
        typ = ov.get("type")
        if typ == "boolean":
            takes_value = False
        elif typ in ("string", "integer", "number", "array"):
            takes_value = True
        else:
            # Heuristic when type omitted: treat as value flag unless enum-only bool-ish names
            takes_value = True
        desc = str(ov.get("description") or key)
        flags.append(CliFlag(names=[f"--{key}"], takes_value=takes_value, description=desc))
    return flags


def discover_flags(cfg: BridgeConfig) -> list[CliFlag]:
    if not cfg.parse_help:
        return _flags_from_whitelist(cfg)
    try:
        proc = subprocess.run(
            [*cfg.command, *cfg.help_argv],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # Do not crash the bridge: fall back so MCP still comes up.
        sys.stderr.write(
            f"[cli-bridge] help discovery failed ({exc}); "
            f"synthesizing flags from whitelist/overrides\n"
        )
        return _flags_from_whitelist(cfg)
    text = (proc.stdout or "") + (proc.stderr or "")
    parsed = parse_help_text(text)
    if not parsed and (cfg.whitelist or cfg.overrides):
        sys.stderr.write(
            "[cli-bridge] help produced no flags; synthesizing from whitelist/overrides\n"
        )
        return _flags_from_whitelist(cfg)
    return parsed


def _normalize_key(key: str) -> str:
    return key.lstrip("-")


def _allowed(flag: CliFlag, cfg: BridgeConfig) -> bool:
    keys = {_normalize_key(k) for k in _flag_keys(flag)}
    if cfg.blacklist:
        blocked = {_normalize_key(b) for b in cfg.blacklist}
        if keys & blocked:
            return False
    if cfg.whitelist:
        allowed = {_normalize_key(w) for w in cfg.whitelist}
        return bool(keys & allowed)
    return True


def _schema_for_flag(flag: CliFlag, cfg: BridgeConfig) -> dict[str, Any]:
    key = flag.primary
    ov = cfg.overrides.get(key, {})
    schema: dict[str, Any] = {"type": ov.get("type", "string" if flag.takes_value else "boolean")}
    if "enum" in ov:
        schema["enum"] = ov["enum"]
    if "description" in ov:
        schema["description"] = ov["description"]
    elif flag.description:
        schema["description"] = flag.description
    if ov.get("default") is not None:
        schema["default"] = ov["default"]
    if not flag.takes_value and schema["type"] == "boolean":
        schema = {"type": "boolean", "description": schema.get("description", "")}
    return schema


def _param_name(flag: CliFlag, cfg: BridgeConfig) -> str:
    key = flag.primary
    ov = cfg.overrides.get(key, {})
    return ov.get("name", key.replace("-", "_"))


def _working_directory_spec(cfg: BridgeConfig) -> dict[str, Any]:
    if cfg.working_directory:
        return cfg.working_directory
    if cfg.work_dir:
        return {"mode": "fixed", "path": cfg.work_dir, "mkdir": True}
    return {}


def _wd_request_param(cfg: BridgeConfig) -> str | None:
    wd = _working_directory_spec(cfg)
    return wd.get("param", "working_directory") if wd.get("mode") == "request" else None


def _wd_maps_to_flag(cfg: BridgeConfig) -> str | None:
    wd = _working_directory_spec(cfg)
    if wd.get("mode") == "request":
        return wd.get("maps_to_flag")
    return None


def _expand_path(path: str) -> str:
    return str(Path(path).expanduser().resolve())


def _ensure_dir(path: str) -> str:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return str(p.resolve())


def _validate_under_roots(path: str, roots: list[str]) -> str:
    if not roots:
        raise ValueError("request working_directory requires allowed_roots")
    resolved = Path(path).resolve()
    for root in roots:
        r = Path(root).expanduser().resolve()
        if resolved == r or r in resolved.parents:
            return str(resolved)
    raise ValueError(f"working directory {path!r} not under allowed_roots")


def _caller_cwd_param_names(cfg: BridgeConfig) -> set[str]:
    """Parameter names a caller might use to override cwd (fixed mode rejects these)."""
    names = {"working_directory", "work_dir", "cwd"}
    wd = _working_directory_spec(cfg)
    if wd.get("param"):
        names.add(wd["param"])
    return names


def _reject_caller_working_directory_override(cfg: BridgeConfig, params: dict[str, Any]) -> None:
    wd = _working_directory_spec(cfg)
    if wd.get("mode", "fixed") != "fixed":
        return
    for name in _caller_cwd_param_names(cfg):
        if params.get(name) not in (None, ""):
            configured = wd.get("path") or cfg.work_dir or "(not configured)"
            raise ValueError(
                f"{name!r} cannot be set by callers; working directory is "
                f"server-configured ({configured!r})"
            )


def _op_requires_working_directory(cfg: BridgeConfig, op: dict[str, Any]) -> bool:
    explicit = op.get("requires_working_directory")
    if explicit is not None:
        return bool(explicit)
    wd = _working_directory_spec(cfg)
    if wd.get("mode") != "request":
        return False
    if op.get("argv") or op.get("argv_template"):
        return False
    return bool(wd.get("required", False))


def _resolve_run_cwd(
    cfg: BridgeConfig, params: dict[str, Any], *, required: bool | None = None
) -> str | None:
    wd = _working_directory_spec(cfg)
    mode = wd.get("mode", "fixed")
    if mode == "fixed":
        path = wd.get("path") or cfg.work_dir
        if not path:
            return None
        path = _expand_path(path)
        return _ensure_dir(path) if wd.get("mkdir", True) else path
    if mode == "request":
        param = wd.get("param", "working_directory")
        val = params.get(param)
        if val in (None, ""):
            val = wd.get("default") or cfg.work_dir
        must_have = wd.get("required", False) if required is None else required
        if not val:
            if must_have:
                raise ValueError(f"missing required parameter: {param}")
            return None
        path = _validate_under_roots(_expand_path(str(val)), wd.get("allowed_roots", []))
        return _ensure_dir(path) if wd.get("mkdir", False) else path
    raise ValueError(f"unknown working_directory.mode: {mode!r}")


def _working_directory_openapi(cfg: BridgeConfig) -> dict[str, Any] | None:
    wd = _working_directory_spec(cfg)
    if wd.get("mode") != "request":
        return None
    param = wd.get("param", "working_directory")
    return {
        "type": "string",
        "description": wd.get(
            "description",
            "Project directory for this invocation (must be under allowed_roots)",
        ),
        "default": wd.get("default"),
    }


def _op_allowed_param_names(
    cfg: BridgeConfig, op: dict[str, Any], flags: list[CliFlag]
) -> set[str]:
    if op.get("type") == "upload_file":
        return set(op.get("upload_fields", ["filename", "content"]))
    if op.get("type") in ("delete_file", "download_file"):
        return set(op.get("path_params", ["filename"]))
    if op.get("type") == "list_files":
        names: set[str] = set()
        wd_param = _wd_request_param(cfg)
        if wd_param and _op_requires_working_directory(cfg, op):
            names.add(wd_param)
        return names
    names: set[str] = set(op.get("include_body_params", list(cfg.body_params.keys())))
    for alt in op.get("input_one_of", []):
        names.add(alt)
    include = set(op.get("include_flags", [f.primary for f in flags]))
    for flag in flags:
        if flag.primary in include:
            names.add(_param_name(flag, cfg))
            names.add(flag.primary)
    out_cfg = op.get("output_format") or {}
    if out_cfg.get("param") and out_cfg.get("expose", True):
        names.add(out_cfg["param"])
    wd_param = _wd_request_param(cfg)
    if wd_param and _op_requires_working_directory(cfg, op):
        names.add(wd_param)
    return names


def _sandbox_suffixes(cfg: BridgeConfig, purpose: str = "any") -> list[str]:
    sf = cfg.sandbox_files
    if purpose == "upload":
        return sf.get("upload_suffixes") or sf.get("allowed_suffixes", [])
    return sf.get("allowed_suffixes", [])


def _resource_dirs(cfg: BridgeConfig) -> list[str]:
    """Relative sandbox subdirs exposed for list/download/MCP resources (default: root only)."""
    dirs = cfg.sandbox_files.get("resource_dirs")
    if dirs is None:
        return ["."]
    out: list[str] = []
    for d in dirs:
        rel = str(d).strip().strip("/\\")
        out.append("." if rel in ("", ".") else rel)
    return out or ["."]


def _sandbox_public_url(cfg: BridgeConfig, relpath: str) -> str | None:
    """Public HTTPS URL for a sandbox file (MCP_PUBLIC_URL + optional public_slug)."""
    prefix = os.getenv("MCP_PUBLIC_URL") or cfg.sandbox_files.get("public_url_prefix")
    if not prefix:
        return None
    prefix = str(prefix).rstrip("/")
    rel = str(relpath).lstrip("/")
    slug = os.getenv("SANDBOX_PUBLIC_SLUG") or cfg.sandbox_files.get("public_slug")
    if slug:
        return f"{prefix}/sandbox/{str(slug).strip('/')}/{rel}"
    return f"{prefix}/sandbox/{rel}"


def _sandbox_root(cfg: BridgeConfig, run_cwd: str | None = None) -> Path:
    root = run_cwd or _resolve_run_cwd(cfg, {})
    if not root:
        raise ValueError("working_directory is required for sandbox file operations")
    return Path(root).resolve()


def _safe_filename(cfg: BridgeConfig, filename: str, *, purpose: str = "any") -> str:
    """Bare sandbox filename (uploads stay at sandbox root)."""
    name = Path(str(filename)).name
    if not name or name in {".", ".."}:
        raise ValueError("invalid filename")
    if ".." in str(filename) or "/" in str(filename) or "\\" in str(filename):
        raise ValueError("filename must be a bare name, not a path")
    suffixes = _sandbox_suffixes(cfg, purpose)
    if suffixes and not any(name.endswith(s) for s in suffixes):
        raise ValueError(f"filename must end with one of {suffixes}")
    return name


def _safe_relative_path(cfg: BridgeConfig, relpath: str, *, purpose: str = "any") -> str:
    """Sandbox-relative path (e.g. previews/bracket.png) under configured resource_dirs."""
    raw = str(relpath).strip().replace("\\", "/")
    if not raw or raw in (".", ".."):
        raise ValueError("invalid path")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise ValueError("path must stay inside the sandbox")
    rel = "/".join(parts)
    suffixes = _sandbox_suffixes(cfg, purpose)
    if suffixes and not any(rel.endswith(s) for s in suffixes):
        raise ValueError(f"path must end with one of {suffixes}")
    allowed_dirs = _resource_dirs(cfg)
    if "." in allowed_dirs:
        return rel
    top = parts[0]
    if top not in allowed_dirs and not any(rel == d or rel.startswith(f"{d}/") for d in allowed_dirs):
        raise ValueError(f"path must be under one of {allowed_dirs}")
    return rel


def _sandbox_file_path(
    cfg: BridgeConfig, filename: str, run_cwd: str | None = None, *, purpose: str = "any"
) -> Path:
    root = _sandbox_root(cfg, run_cwd)
    if "/" in str(filename).replace("\\", "/"):
        rel = _safe_relative_path(cfg, filename, purpose=purpose)
        candidate = (root / rel).resolve()
        if not str(candidate).startswith(str(root)):
            raise ValueError(f"path escapes sandbox ({root}): {filename}")
        return candidate
    return Path(_sandbox_path(_safe_filename(cfg, filename, purpose=purpose), str(root)))


def _relative_sandbox_name(cfg: BridgeConfig, path: str | Path, run_cwd: str | None = None) -> str:
    root = _sandbox_root(cfg, run_cwd)
    return str(Path(path).resolve().relative_to(root)).replace("\\", "/")


def _output_format_value(cfg: BridgeConfig, op: dict[str, Any], params: dict[str, Any]) -> str:
    out_cfg = op.get("output_format") or {}
    fmt = params.get(out_cfg.get("param", "output_format"), out_cfg.get("default", "out"))
    if not isinstance(fmt, str) or not fmt:
        fmt = out_cfg.get("default", "out")
    allowed = out_cfg.get("enum")
    if allowed and fmt not in allowed:
        raise ValueError(f"output_format must be one of {allowed}, got {fmt!r}")
    return fmt.lstrip(".")


def _derive_output_name(body: dict[str, Any], fmt: str) -> str:
    ext = fmt.lstrip(".")
    if body.get("output_name") not in (None, ""):
        raw = str(body["output_name"]).strip().replace("\\", "/")
        if "/" in raw:
            parent = "/".join(raw.split("/")[:-1])
            stem = Path(raw).stem
            return f"{parent}/{stem}.{ext}" if parent else f"{stem}.{ext}"
        return f"{Path(raw).stem}.{ext}"
    if body.get("filename") not in (None, ""):
        raw = str(body["filename"]).strip().replace("\\", "/")
        if "/" in raw:
            parent = "/".join(raw.split("/")[:-1])
            stem = Path(raw).stem
            return f"{parent}/{stem}.{ext}" if parent else f"{stem}.{ext}"
        return f"{Path(raw).stem}.{ext}"
    return f"output.{ext}"


def _resolve_output_path(
    cfg: BridgeConfig,
    op: dict[str, Any],
    params: dict[str, Any],
    body: dict[str, Any] | None,
    run_cwd: str | None,
) -> tuple[str, bool]:
    """Return (output path, persist in sandbox after run)."""
    fmt = _output_format_value(cfg, op, params)
    if op.get("persist_output"):
        out_name = _derive_output_name(body or {}, fmt)
        out_path = _sandbox_file_path(cfg, out_name, run_cwd, purpose="any")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        return str(out_path), True
    suffix = f".{fmt}"
    fd, path = tempfile.mkstemp(suffix=suffix, dir=run_cwd)
    os.close(fd)
    return path, False


def _list_sandbox_files(cfg: BridgeConfig, run_cwd: str | None = None) -> list[dict[str, Any]]:
    root = _sandbox_root(cfg, run_cwd)
    root.mkdir(parents=True, exist_ok=True)
    suffixes = _sandbox_suffixes(cfg)
    recursive = bool(cfg.sandbox_files.get("list_recursive", True))
    files: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_file(path: Path) -> None:
        if not path.is_file():
            return
        if suffixes and not any(path.name.endswith(s) for s in suffixes):
            return
        rel = str(path.relative_to(root)).replace("\\", "/")
        if rel in seen:
            return
        seen.add(rel)
        st = path.stat()
        files.append({"filename": rel, "size": st.st_size, "modified": int(st.st_mtime)})

    for rel_dir in _resource_dirs(cfg):
        base = root if rel_dir == "." else root / rel_dir
        base.mkdir(parents=True, exist_ok=True)
        if not base.is_dir():
            continue
        if recursive:
            for path in sorted(base.rglob("*")):
                add_file(path)
        else:
            for path in sorted(base.iterdir()):
                add_file(path)
    return sorted(files, key=lambda item: item["filename"])


def _upload_sandbox_file(cfg: BridgeConfig, filename: str, content: str, run_cwd: str | None = None) -> dict[str, Any]:
    max_bytes = int(cfg.sandbox_files.get("max_bytes", 2_000_000))
    if len(content.encode()) > max_bytes:
        raise ValueError(f"file exceeds max_bytes ({max_bytes})")
    path = _sandbox_file_path(cfg, filename, run_cwd, purpose="upload")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return {"filename": path.name, "path": str(path), "size": path.stat().st_size}


def _download_sandbox_file(cfg: BridgeConfig, filename: str, run_cwd: str | None = None) -> dict[str, Any]:
    path = _sandbox_file_path(cfg, filename, run_cwd)
    if not path.is_file():
        raise ValueError(f"file not found: {path.name}")
    data = path.read_bytes()
    output_file = _build_output_file(data, str(path), cfg)
    rel = _relative_sandbox_name(cfg, path, run_cwd)
    payload: dict[str, Any] = {
        "filename": rel,
        "path": str(path),
        "size": len(data),
        "output_file": output_file,
        "sandbox_file": {"filename": rel, "path": str(path), "size": len(data)},
    }
    url = _sandbox_public_url(cfg, rel)
    if url:
        payload["url"] = url
        output_file["url"] = url
    if cfg.inline_binary_outputs:
        payload["content"] = _mcp_content_blocks(
            output_file["mime_type"], output_file["base64"], output_file["size"]
        )
    return payload


def _delete_sandbox_file(cfg: BridgeConfig, filename: str, run_cwd: str | None = None) -> dict[str, Any]:
    path = _sandbox_file_path(cfg, filename, run_cwd)
    if not path.is_file():
        raise ValueError(f"file not found: {path.name}")
    size = path.stat().st_size
    path.unlink()
    return {"deleted": path.name, "size": size}


def _resolve_render_input(
    cfg: BridgeConfig,
    op: dict[str, Any],
    body: dict[str, Any] | None,
    temp_files: list[str],
    run_cwd: str | None = None,
) -> str:
    body = body or {}
    one_of = op.get("input_one_of", [])
    present = [name for name in one_of if body.get(name) not in (None, "")]
    if len(present) != 1:
        raise ValueError(f"provide exactly one of: {', '.join(one_of)}")

    choice = present[0]
    if choice in cfg.body_params:
        meta = cfg.body_params[choice]
        val = body[choice]
        if meta.get("write_temp"):
            suffix = meta.get("suffix", ".tmp")
            fd, path = tempfile.mkstemp(suffix=suffix, dir=run_cwd)
            os.close(fd)
            Path(path).write_text(val if isinstance(val, str) else json.dumps(val))
            temp_files.append(path)
            return path
        if meta.get("use_sandbox_file"):
            return str(_sandbox_file_path(cfg, str(val), run_cwd))
        raise ValueError(f"unsupported body param mode for {choice}")

    if choice == "filename":
        return str(_sandbox_file_path(cfg, str(body["filename"]), run_cwd))

    raise ValueError(f"unknown input selector: {choice}")


def _sandbox_path(path: str, work_dir: str | None) -> str:
    if not work_dir:
        return path
    root = Path(work_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"path escapes sandbox ({work_dir}): {path}")
    return str(resolved)


def _blocked_argv_tokens(cfg: BridgeConfig) -> set[str]:
    blocked: set[str] = set()
    for item in cfg.blacklist or []:
        key = _normalize_key(item)
        blocked.add(f"-{key}")
        blocked.add(f"--{key}")
    return blocked


def _assert_safe_argv(argv: list[str], cfg: BridgeConfig, user_params: dict[str, Any] | None = None) -> None:
    """Reject argv tokens that map to blacklisted flags requested by the caller."""
    blocked = _blocked_argv_tokens(cfg)
    if not blocked:
        return
    user_keys = {_normalize_key(k) for k in (user_params or {})}
    for token in argv:
        if token not in blocked:
            continue
        key = token.lstrip("-")
        if user_keys & {key, key.replace("-", "_")}:
            raise ValueError(f"blocked CLI flag requested: {token}")


def _find_flag(flags: list[CliFlag], key: str) -> CliFlag | None:
    want = _normalize_key(key)
    for flag in flags:
        if flag.primary == want:
            return flag
        if want in {_normalize_key(n) for n in flag.names}:
            return flag
    return None


def _argv_for_flag_value(flags: list[CliFlag], flag_key: str, value: Any) -> list[str]:
    flag = _find_flag(flags, flag_key)
    if flag and not flag.takes_value:
        return [next((n for n in flag.names if n.startswith("--")), f"--{flag_key}")]
    long = f"--{flag_key.replace('_', '-')}"
    if flag:
        long = next((n for n in flag.names if n.startswith("--")), long)
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend([long, str(item)])
        return out
    return [long, str(value)]


def _resolve_fixed_flags(op: dict[str, Any], flags: list[CliFlag]) -> list[str]:
    argv: list[str] = []
    for key, val in (op.get("fixed_flags") or {}).items():
        if val in (False, None, "", 0):
            continue
        if val is True:
            argv.extend(_argv_for_flag_value(flags, key, True)[:1])
        else:
            argv.extend(_argv_for_flag_value(flags, key, val))
    return argv


def _mapped_flag_keys(cfg: BridgeConfig) -> set[str]:
    keys: set[str] = set()
    for meta in cfg.body_params.values():
        if meta.get("maps_to_flag"):
            keys.add(_normalize_key(meta["maps_to_flag"]))
    return keys


def _output_file_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "description": "Rendered file returned inline; ephemeral server paths are not hosted.",
        "properties": {
            "mime_type": {"type": "string"},
            "size": {"type": "integer"},
            "base64": {
                "type": "string",
                "description": "File bytes base64-encoded for the caller to decode and save/display locally.",
            },
            "data_uri": {
                "type": "string",
                "description": "data:<mime>;base64,... suitable for inline image display",
            },
            "filename": {
                "type": "string",
                "description": "Suggested local filename (extension from output format)",
            },
        },
    }


def _mcp_content_blocks(
    mime_type: str,
    b64: str,
    size: int,
    *,
    public_url: str | None = None,
    filename: str | None = None,
) -> list[dict[str, Any]]:
    """MCP CallToolResult content blocks (proxy expands these to ImageContent)."""
    blocks: list[dict[str, Any]] = []
    if mime_type.startswith("image/"):
        data_uri = f"data:{mime_type};base64,{b64}"
        blocks.append({"type": "image", "data": b64, "mimeType": mime_type})
        # Resource link is primarily added by the proxy from sandbox metadata; bridge
        # text still mentions the public URL when present so non-proxy clients see it.
        if public_url:
            name = filename or "output"
            text = (
                f"![OpenSCAD preview]({data_uri})\n\n"
                f"Rendered `{name}` ({mime_type}, {size} bytes).\n\n"
                f"Also saved — Preview URL: {public_url}"
            )
        else:
            text = f"![OpenSCAD preview]({data_uri})\n\nRendered {mime_type} ({size} bytes)."
        blocks.append({"type": "text", "text": text})
    else:
        blocks.append({
            "type": "text",
            "text": (
                f"Output file ({mime_type}, {size} bytes) returned inline in "
                "output_file.base64."
            ),
        })
    return blocks


def _build_output_metadata(
    cfg: BridgeConfig, data: bytes, candidate: str, *, relpath: str | None = None
) -> dict[str, Any]:
    mime_type = mimetypes.guess_type(candidate)[0] or "application/octet-stream"
    filename = relpath or Path(candidate).name
    meta: dict[str, Any] = {
        "mime_type": mime_type,
        "size": len(data),
        "filename": filename,
        "sandbox_uri": f"sandbox://{filename}",
    }
    url = _sandbox_public_url(cfg, filename)
    if url:
        meta["url"] = url
    return meta


def _return_inline(
    body: dict[str, Any] | None,
    cfg: BridgeConfig,
    op: dict[str, Any] | None = None,
) -> bool:
    """Whether to embed binary bytes in the tool response.

    Precedence: explicit body ``return_inline`` → operation ``return_inline_default``
    → body_params.return_inline.default → cfg.inline_binary_outputs.
    """
    if body is not None and "return_inline" in body:
        return bool(body["return_inline"])
    if op is not None and "return_inline_default" in op:
        return bool(op["return_inline_default"])
    meta = cfg.body_params.get("return_inline", {})
    if "default" in meta:
        return bool(meta["default"])
    return cfg.inline_binary_outputs


def _saved_file_message(meta: dict[str, Any]) -> str:
    header = (
        f"Saved `{meta['filename']}` ({meta['mime_type']}, {meta['size']} bytes) to sandbox."
    )
    url = meta.get("url")
    if url:
        if str(meta.get("mime_type", "")).startswith("image/"):
            return f"{header}\n\n![OpenSCAD preview]({url})\n\nPreview URL: {url}"
        return f"{header}\n\nDownload URL: {url}"
    return (
        f"{header} Fetch via resources/read `{meta['sandbox_uri']}` or download_file."
    )


def _build_output_file(
    data: bytes,
    candidate: str,
    cfg: BridgeConfig,
    *,
    relpath: str | None = None,
) -> dict[str, Any]:
    mime_type = mimetypes.guess_type(candidate)[0] or "application/octet-stream"
    b64 = base64.b64encode(data).decode("ascii")
    filename = relpath or Path(candidate).name
    payload: dict[str, Any] = {
        "mime_type": mime_type,
        "size": len(data),
        "base64": b64,
        "filename": filename,
        "sandbox_uri": f"sandbox://{filename}",
    }
    if mime_type.startswith("image/"):
        payload["data_uri"] = f"data:{mime_type};base64,{b64}"
    url = _sandbox_public_url(cfg, filename)
    if url:
        payload["url"] = url
    if not cfg.inline_binary_outputs:
        payload["path"] = candidate
    return payload


def _auto_output_path(cfg: BridgeConfig, op: dict[str, Any], params: dict[str, Any], run_cwd: str | None = None) -> str:
    path, _ = _resolve_output_path(cfg, op, params, None, run_cwd)
    return path


def _wd_query_parameter(cfg: BridgeConfig, op: dict[str, Any]) -> dict[str, Any] | None:
    if not _op_requires_working_directory(cfg, op):
        return None
    schema = _working_directory_openapi(cfg)
    if not schema:
        return None
    param = _wd_request_param(cfg) or "working_directory"
    return {
        "name": param,
        "in": "query",
        "required": True,
        "schema": schema,
    }


def _wd_schema_props(
    cfg: BridgeConfig, op: dict[str, Any], body_names: list[str]
) -> tuple[dict[str, Any], list[str]]:
    props: dict[str, Any] = {}
    required: list[str] = []
    if not _op_requires_working_directory(cfg, op):
        return props, required
    schema = _working_directory_openapi(cfg)
    if not schema:
        return props, required
    param = _wd_request_param(cfg) or "working_directory"
    if param not in body_names:
        body_names.append(param)
    props[param] = schema
    required.append(param)
    return props, required


def default_operations(cfg: BridgeConfig, flags: list[CliFlag]) -> list[dict[str, Any]]:
    return [
        {
            "id": "execute",
            "path": "/execute",
            "method": "post",
            "summary": f"Run {cfg.title} with whitelisted flags",
            "include_flags": [f.primary for f in flags],
            "include_body_params": list(cfg.body_params.keys()),
        }
    ]


def build_openapi(cfg: BridgeConfig, flags: list[CliFlag] | None = None) -> dict[str, Any]:
    if flags is None:
        flags = discover_flags(cfg)
    flags = [f for f in flags if _allowed(f, cfg)]
    ops = _with_run_operation(cfg, cfg.operations or default_operations(cfg, flags))

    paths: dict[str, Any] = {}
    for op in ops:
        method = op.get("method", "post").lower()
        path = op["path"]
        parameters: list[dict[str, Any]] = []
        request_body: dict[str, Any] | None = None
        op_type = op.get("type", "cli")

        if op_type == "run":
            sample_prefix = next(
                (p for p in _defined_argv_prefixes(cfg) if p),
                ["--version"],
            )
            props: dict[str, Any] = {
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Argv tokens appended after the configured command "
                        f"(e.g. {json.dumps(sample_prefix)}). "
                        "Must start with a defined operation argv prefix when those exist."
                    ),
                },
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Alias for args",
                },
            }
            if _arbitrary_command_allowed():
                props["command"] = {
                    "type": "string",
                    "description": (
                        "Absolute path override (or JSON array of strings). "
                        "Only accepted when CLI_BRIDGE_ALLOW_ARBITRARY_COMMAND=1."
                    ),
                }
            # Optional request-mode cwd (required only at runtime for matching tools that need it)
            wd_schema = _working_directory_openapi(cfg)
            if wd_schema:
                param = _wd_request_param(cfg) or "working_directory"
                props[param] = {
                    **wd_schema,
                    "description": (
                        (wd_schema.get("description") or "Working directory")
                        + " Required when the matched tool prefix requires a cwd "
                        "(e.g. repo-aware gh subcommands)."
                    ),
                }
            request_body = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": props,
                            "required": ["args"],
                        }
                    }
                },
            }
        elif op_type == "upload_file":
            fields = op.get("upload_fields", ["filename", "content"])
            props = {
                "filename": {"type": "string", "description": "Bare filename stored in the sandbox (e.g. part.scad)"},
                "content": {"type": "string", "description": "File contents"},
            }
            upload_required = list(fields)
            wd_props, wd_required = _wd_schema_props(cfg, op, list(fields))
            props.update(wd_props)
            upload_required.extend(wd_required)
            request_body = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {k: props[k] for k in props},
                            "required": upload_required or None,
                        }
                    }
                },
            }
        elif op_type in ("delete_file", "download_file"):
            pname = op.get("path_params", ["filepath"])[0]
            parameters.append({
                "name": pname,
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "description": "Sandbox-relative path (e.g. bracket.png or previews/bracket.png)",
                },
            })
        elif op_type == "list_files":
            wd_query = _wd_query_parameter(cfg, op)
            if wd_query:
                parameters.append(wd_query)
        elif op.get("argv") or op.get("argv_template"):
            # Fixed subcommand argv (e.g. gh issue list) may still expose body/query flags.
            include = set(op.get("include_flags") or [])
            mapped = _mapped_flag_keys(cfg)
            wd_flag = _wd_maps_to_flag(cfg)
            if wd_flag:
                mapped.add(_normalize_key(wd_flag))
            for flag in flags:
                if flag.primary not in include or flag.primary in mapped:
                    continue
                pname = _param_name(flag, cfg)
                ov = cfg.overrides.get(flag.primary, {})
                loc = ov.get("in", "query")
                if loc == "query":
                    parameters.append({
                        "name": pname,
                        "in": "query",
                        "required": ov.get("required", False),
                        "schema": _schema_for_flag(flag, cfg),
                    })
            body_names = list(op.get("include_body_params") or [])
            # Prefer JSON body for POST so clients can pass working_directory + flags together.
            if body_names or _op_requires_working_directory(cfg, op):
                props: dict[str, Any] = {}
                required: list[str] = []
                for name in body_names:
                    meta = cfg.body_params.get(name, {})
                    if not meta and name in include:
                        # flag-only body param synthesized from whitelist flag
                        fl = next((f for f in flags if f.primary == name or _param_name(f, cfg) == name), None)
                        if fl:
                            props[name] = _schema_for_flag(fl, cfg)
                            continue
                    prop: dict[str, Any] = {
                        "type": meta.get("type", "string"),
                        "description": meta.get("description", ""),
                    }
                    if "enum" in meta:
                        prop["enum"] = meta["enum"]
                    if "default" in meta:
                        prop["default"] = meta["default"]
                    props[name] = prop
                    if meta.get("required"):
                        required.append(name)
                wd_props, wd_required = _wd_schema_props(cfg, op, list(props.keys()))
                props.update(wd_props)
                required.extend(wd_required)
                if props:
                    request_body = {
                        "required": bool(required),
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": props,
                                    "required": required or None,
                                }
                            }
                        },
                    }
            else:
                wd_query = _wd_query_parameter(cfg, op)
                if wd_query:
                    parameters.append(wd_query)
        else:
            mapped = _mapped_flag_keys(cfg)
            wd_flag = _wd_maps_to_flag(cfg)
            if wd_flag:
                mapped.add(_normalize_key(wd_flag))
            include = set(op.get("include_flags", [f.primary for f in flags]))
            for flag in flags:
                if flag.primary not in include or flag.primary in mapped:
                    continue
                pname = _param_name(flag, cfg)
                ov = cfg.overrides.get(flag.primary, {})
                loc = ov.get("in", "query")
                param = {
                    "name": pname,
                    "in": loc,
                    "required": ov.get("required", False),
                    "schema": _schema_for_flag(flag, cfg),
                }
                if loc == "query":
                    parameters.append(param)

            body_names = list(op.get("include_body_params", list(cfg.body_params.keys())))
            out_cfg = op.get("output_format") or {}
            out_param = out_cfg.get("param")
            if (
                op.get("auto_output")
                and out_param
                and out_cfg.get("expose", True)
                and out_param not in body_names
            ):
                body_names.append(out_param)
            if body_names:
                props: dict[str, Any] = {}
                required: list[str] = []
                for name in body_names:
                    if name == out_param and op.get("auto_output"):
                        props[name] = {
                            "type": "string",
                            "enum": out_cfg.get("enum"),
                            "default": out_cfg.get("default"),
                            "description": out_cfg.get(
                                "description",
                                "Output format; file is written inside the bridge sandbox.",
                            ),
                        }
                        if out_cfg.get("required", True):
                            required.append(name)
                        continue
                    meta = cfg.body_params.get(name, {})
                    prop: dict[str, Any] = {
                        "type": meta.get("type", "string"),
                        "description": meta.get("description", ""),
                    }
                    if "default" in meta:
                        prop["default"] = meta["default"]
                    if meta.get("type") == "boolean":
                        prop["type"] = "boolean"
                    props[name] = prop
                    if meta.get("required", False) and not op.get("input_one_of"):
                        required.append(name)
                if op.get("input_one_of"):
                    for alt in op["input_one_of"]:
                        if alt in props:
                            continue
                        if alt == "filename":
                            props[alt] = {
                                "type": "string",
                                "description": "Render an existing sandbox file by name (for include/use chains)",
                            }
                        else:
                            meta = cfg.body_params.get(alt, {})
                            props[alt] = {
                                "type": meta.get("type", "string"),
                                "description": meta.get("description", ""),
                            }
                wd_props, wd_required = _wd_schema_props(cfg, op, body_names)
                props.update(wd_props)
                required.extend(wd_required)
                request_body = {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": props,
                                "required": required or None,
                            }
                        }
                    },
                }

        entry = paths.setdefault(path, {})
        response_schema: dict[str, Any]
        if op_type == "upload_file":
            response_schema = {
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "path": {"type": "string"},
                    "size": {"type": "integer"},
                },
            }
        elif op_type == "delete_file":
            response_schema = {
                "type": "object",
                "properties": {
                    "deleted": {"type": "string"},
                    "size": {"type": "integer"},
                },
            }
        elif op_type == "list_files":
            response_schema = {
                "type": "object",
                "properties": {
                    "files": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "filename": {"type": "string"},
                                "size": {"type": "integer"},
                                "modified": {"type": "integer"},
                            },
                        },
                    }
                },
            }
        elif op_type == "download_file":
            response_schema = {
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "path": {"type": "string"},
                    "size": {"type": "integer"},
                    "output_file": _output_file_schema(),
                    "sandbox_file": {
                        "type": "object",
                        "properties": {
                            "filename": {"type": "string"},
                            "path": {"type": "string"},
                            "size": {"type": "integer"},
                        },
                    },
                    "content": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string"},
                                "text": {"type": "string"},
                                "data": {"type": "string"},
                                "mimeType": {"type": "string"},
                            },
                        },
                    },
                },
            }
        else:
            response_schema = {
                "type": "object",
                "properties": {
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                    "exit_code": {"type": "integer"},
                    "output_file": _output_file_schema(),
                    "sandbox_file": {
                        "type": "object",
                        "properties": {
                            "filename": {"type": "string"},
                            "path": {"type": "string"},
                            "size": {"type": "integer"},
                        },
                    },
                    "content": {
                        "type": "array",
                        "description": "MCP-style inline content blocks (images include base64 data)",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string"},
                                "text": {"type": "string"},
                                "data": {"type": "string"},
                                "mimeType": {"type": "string"},
                            },
                        },
                    },
                },
            }
        entry[method] = {
            "operationId": op.get("id", path.strip("/").replace("/", "_") or "root"),
            "summary": op.get("summary", ""),
            "description": op.get("description", ""),
            "parameters": parameters or None,
            "requestBody": request_body,
            "responses": {
                "200": {
                    "description": "ok",
                    "content": {"application/json": {"schema": response_schema}},
                }
            },
        }
        if not parameters:
            entry[method].pop("parameters", None)
        if not request_body:
            entry[method].pop("requestBody", None)

    return {
        "openapi": "3.0.3",
        "info": {"title": cfg.title, "version": cfg.version, "description": f"HTTP bridge for {' '.join(cfg.command)}"},
        "servers": [{"url": "http://localhost"}],
        "paths": paths,
    }


def _resolve_flag(flag: CliFlag, params: dict[str, Any], cfg: BridgeConfig) -> list[str]:
    pname = _param_name(flag, cfg)
    if pname not in params and flag.primary not in params:
        return []
    val = params.get(pname, params.get(flag.primary))
    if val is None:
        return []
    if not flag.takes_value:
        if val in (True, "true", "1", 1):
            long = next((n for n in flag.names if n.startswith("--")), flag.names[0])
            return [long]
        return []
    long = next((n for n in flag.names if n.startswith("--")), flag.names[0])
    if isinstance(val, list):
        out: list[str] = []
        for item in val:
            out.extend([long, str(item)])
        return out
    return [long, str(val)]


def execute_operation(
    cfg: BridgeConfig,
    op: dict[str, Any],
    flags: list[CliFlag],
    query: dict[str, list[str]],
    body: dict[str, Any] | None,
    path_params: dict[str, str] | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for k, vals in query.items():
        if len(vals) == 1:
            params[k] = vals[0]
        else:
            params[k] = vals
    if body:
        params.update(body)
    if path_params:
        params.update(path_params)

    _reject_caller_working_directory_override(cfg, params)

    op_type = op.get("type", "cli")
    # /run resolves cwd after prefix matching (per-tool requires_working_directory).
    if op_type == "run":
        return _execute_run(cfg, body, params)

    run_cwd = _resolve_run_cwd(cfg, params, required=_op_requires_working_directory(cfg, op))

    if op_type == "upload_file":
        fields = op.get("upload_fields", ["filename", "content"])
        missing = [f for f in fields if not body or body.get(f) in (None, "")]
        if missing:
            raise ValueError(f"missing upload fields: {missing}")
        return _upload_sandbox_file(cfg, str(body["filename"]), str(body["content"]), run_cwd)
    if op_type == "delete_file":
        pname = op.get("path_params", ["filepath"])[0]
        if pname not in (path_params or {}):
            raise ValueError(f"missing path param: {pname}")
        return _delete_sandbox_file(cfg, path_params[pname], run_cwd)
    if op_type == "download_file":
        pname = op.get("path_params", ["filepath"])[0]
        if pname not in (path_params or {}):
            raise ValueError(f"missing path param: {pname}")
        return _download_sandbox_file(cfg, path_params[pname], run_cwd)
    if op_type == "list_files":
        return {"files": _list_sandbox_files(cfg, run_cwd), "working_directory": run_cwd}

    if cfg.reject_unknown_params and not (op.get("argv") or op.get("argv_template")):
        allowed = _op_allowed_param_names(cfg, op, flags)
        unknown = sorted(set(params) - allowed)
        if unknown:
            raise ValueError(f"unknown parameters not exposed by spec: {unknown}")

    argv = list(cfg.command)
    temp_files: list[str] = []
    output_path: str | None = None

    try:
        subcommand_argv = bool(op.get("argv") or op.get("argv_template"))
        if op.get("argv"):
            argv.extend(op["argv"])
        elif op.get("argv_template"):
            argv.extend(op["argv_template"])

        # Flag/body resolution for normal ops, and optional flags after fixed subcommand argv
        # (e.g. gh issue list --limit 10 --repo owner/name).
        if (not subcommand_argv) or op.get("include_flags") or op.get("include_body_params") or op.get(
            "fixed_flags"
        ):
            all_flags = discover_flags(cfg)
            if not subcommand_argv:
                argv.extend(_resolve_fixed_flags(op, all_flags))
            else:
                argv.extend(_resolve_fixed_flags(op, all_flags))

            include = set(
                op.get("include_flags", [] if subcommand_argv else [f.primary for f in flags])
            )
            mapped = _mapped_flag_keys(cfg)
            wd_flag = _wd_maps_to_flag(cfg)
            if wd_flag:
                mapped.add(_normalize_key(wd_flag))
            for flag in flags:
                if flag.primary in include and flag.primary not in mapped:
                    argv.extend(_resolve_flag(flag, params, cfg))

            wd_param = _wd_request_param(cfg)
            if (not subcommand_argv) and wd_flag and wd_param and params.get(wd_param) not in (None, ""):
                argv.extend(_argv_for_flag_value(all_flags, wd_flag, params[wd_param]))

            if body:
                include_body = set(op.get("include_body_params", [] if subcommand_argv else list(cfg.body_params.keys())))
                for name, meta in cfg.body_params.items():
                    if name not in include_body and subcommand_argv:
                        continue
                    if name not in body or body.get(name) in (None, ""):
                        if meta.get("required") and name in op.get("include_body_params", []):
                            raise ValueError(f"missing required body field: {name}")
                        continue
                    val = body[name]
                    if meta.get("maps_to_flag"):
                        argv.extend(_argv_for_flag_value(all_flags, meta["maps_to_flag"], val))
                        continue
                    if meta.get("cli_flag"):
                        # explicit CLI flag name without whitelist entry (e.g. --method)
                        flag_name = str(meta["cli_flag"])
                        if not flag_name.startswith("-"):
                            flag_name = f"--{flag_name}"
                        argv.extend([flag_name, str(val)])
                        continue
                    if meta.get("positional") == "arg":
                        argv.append(str(val))
                        continue
                    if meta.get("write_temp") or meta.get("positional") == "input":
                        if subcommand_argv:
                            continue
                        continue  # handled below

            if not subcommand_argv:
                if op.get("input_one_of"):
                    argv.append(_resolve_render_input(cfg, op, body, temp_files, run_cwd))
                else:
                    for name, meta in cfg.body_params.items():
                        if name not in (body or {}) or meta.get("maps_to_flag"):
                            continue
                        val = body[name]
                        if meta.get("write_temp"):
                            suffix = meta.get("suffix", ".tmp")
                            fd, path = tempfile.mkstemp(suffix=suffix, dir=run_cwd)
                            os.close(fd)
                            Path(path).write_text(val if isinstance(val, str) else json.dumps(val))
                            temp_files.append(path)
                            if meta.get("positional") == "input":
                                argv.append(path)
                        elif meta.get("positional") == "input":
                            if cfg.sandbox_outputs and run_cwd:
                                raise ValueError("positional input paths must use write_temp body params")
                            argv.append(str(val))

                if op.get("auto_output"):
                    output_path, persist = _resolve_output_path(cfg, op, params, body, run_cwd)
                    if not persist:
                        temp_files.append(output_path)
                    argv.extend(["-o", output_path])
                else:
                    out_key = op.get("returns_file") or cfg.overrides.get("o", {}).get("name", "output")
                    if out_key in params and params[out_key]:
                        output_path = str(params[out_key])
                        if cfg.sandbox_outputs:
                            output_path = _sandbox_path(output_path, run_cwd)
                        if not any(a in ("-o", "--o") for a in argv):
                            argv.extend(["-o", output_path])

        _assert_safe_argv(argv, cfg, params)

        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=cfg.timeout_sec,
            cwd=run_cwd,
        )
        result: dict[str, Any] = {
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "exit_code": proc.returncode,
            "argv": argv,
        }

        candidate = output_path
        if not candidate:
            for i, arg in enumerate(argv):
                if arg in ("-o", "--o") and i + 1 < len(argv):
                    candidate = argv[i + 1]
                    break

        if candidate and os.path.isfile(candidate):
            data = Path(candidate).read_bytes()
            inline = _return_inline(body, cfg, op)
            rel = _relative_sandbox_name(cfg, candidate, run_cwd)
            if inline:
                # Embed bytes (ImageContent / blob) AND keep sandbox resource metadata
                # so clients get base64 by default while files remain fetchable via URL.
                output_file = _build_output_file(data, candidate, cfg, relpath=rel)
                result["output_file"] = output_file
                result["filename"] = rel
                result["sandbox_uri"] = output_file["sandbox_uri"]
                if output_file.get("url"):
                    result["url"] = output_file["url"]
                if op.get("persist_output"):
                    result["sandbox_file"] = {
                        "filename": rel,
                        "path": candidate,
                        "size": output_file["size"],
                    }
                if cfg.inline_binary_outputs:
                    result["content"] = _mcp_content_blocks(
                        output_file["mime_type"],
                        output_file["base64"],
                        output_file["size"],
                        public_url=output_file.get("url"),
                        filename=rel,
                    )
            else:
                meta = _build_output_metadata(cfg, data, candidate, relpath=rel)
                result["output_file"] = meta
                result["filename"] = meta["filename"]
                result["sandbox_uri"] = meta["sandbox_uri"]
                if meta.get("url"):
                    result["url"] = meta["url"]
                if op.get("persist_output"):
                    result["sandbox_file"] = {
                        "filename": rel,
                        "path": candidate,
                        "size": meta["size"],
                    }
                result["content"] = [{"type": "text", "text": _saved_file_message(meta)}]
        return result
    finally:
        for path in temp_files:
            try:
                os.unlink(path)
            except OSError:
                pass


def _match_operation(path: str, method: str, ops: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, str]] | None:
    method = method.lower()
    if path.startswith("/files/") and path != "/files":
        relpath = unquote(path[len("/files/") :])
        want = "download_file" if method == "get" else "delete_file" if method == "delete" else None
        if want:
            for op in ops:
                if op.get("type") == want:
                    pname = op.get("path_params", ["filepath"])[0]
                    return op, {pname: relpath}
    for op in ops:
        if op.get("method", "post").lower() != method:
            continue
        op_path = op["path"]
        if "{" in op_path:
            parts = op_path.strip("/").split("/")
            actual = path.strip("/").split("/")
            if len(parts) != len(actual):
                continue
            captured: dict[str, str] = {}
            ok = True
            for pat, val in zip(parts, actual):
                if pat.startswith("{") and pat.endswith("}"):
                    captured[pat[1:-1]] = val
                elif pat != val:
                    ok = False
                    break
            if ok:
                return op, captured
        elif op_path == path:
            return op, {}
    return None


class BridgeState:
    def __init__(self, cfg: BridgeConfig):
        self.cfg = cfg
        self.flags = discover_flags(cfg)
        self.flags = [f for f in self.flags if _allowed(f, self.cfg)]
        base_ops = cfg.operations or default_operations(cfg, self.flags)
        self.operations = _with_run_operation(cfg, base_ops)
        self.spec = build_openapi(cfg, self.flags)


def _serve_sandbox_static(cfg: BridgeConfig, path: str) -> tuple[int, bytes, str] | tuple[int, dict[str, Any], str]:
    """Serve a sandbox file as raw bytes. Returns (code, body, content_type)."""
    if not path.startswith("/sandbox/"):
        return 404, {"error": "not found"}, "application/json"
    relpath = unquote(path[len("/sandbox/") :])
    if not relpath or relpath.endswith("/"):
        return 404, {"error": "not found"}, "application/json"
    try:
        file_path = _sandbox_file_path(cfg, relpath)
        if not file_path.is_file():
            return 404, {"error": "not found"}, "application/json"
        data = file_path.read_bytes()
        mime = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        return 200, data, mime
    except Exception as exc:
        return 404, {"error": str(exc)}, "application/json"


def make_handler(state: BridgeState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write(f"[cli-bridge] {self.address_string()} - {fmt % args}\n")

        def _send_json(self, code: int, payload: Any) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_bytes(self, code: int, data: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=300")
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            if method == "GET" and parsed.path.startswith("/sandbox/"):
                code, body, content_type = _serve_sandbox_static(state.cfg, parsed.path)
                if content_type == "application/json":
                    return self._send_json(code, body)  # type: ignore[arg-type]
                return self._send_bytes(code, body, content_type)  # type: ignore[arg-type]
            if parsed.path in ("/openapi.json", "/openapi.yaml"):
                return self._send_json(200, state.spec)
            matched = _match_operation(parsed.path, method, state.operations)
            if not matched:
                return self._send_json(404, {"error": "not found"})
            op, path_params = matched
            body = None
            if method in ("POST", "PUT", "PATCH"):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode() or "{}")
                except json.JSONDecodeError:
                    return self._send_json(400, {"error": "invalid JSON body"})
            qs = {k: v for k, v in parse_qs(parsed.query).items()}
            try:
                result = execute_operation(state.cfg, op, state.flags, qs, body, path_params)
                if op.get("type", "cli") in ("cli", "run") and "exit_code" in result:
                    return self._send_json(200 if result["exit_code"] == 0 else 500, result)
                return self._send_json(200, result)
            except subprocess.TimeoutExpired:
                return self._send_json(504, {"error": "cli timeout"})
            except Exception as exc:
                return self._send_json(500, {"error": str(exc)})

        def do_GET(self) -> None:
            return self._dispatch("GET")

        def do_POST(self) -> None:
            return self._dispatch("POST")

        def do_DELETE(self) -> None:
            return self._dispatch("DELETE")

    return Handler


def cmd_generate(args: argparse.Namespace) -> int:
    cfg = BridgeConfig.load(args.config)
    spec = build_openapi(cfg)
    text = json.dumps(spec, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n")
    else:
        print(text)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    cfg = BridgeConfig.load(args.config)
    if _working_directory_spec(cfg).get("mode", "fixed") == "fixed":
        _resolve_run_cwd(cfg, {})
    state = BridgeState(cfg)
    handler = make_handler(state)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"[cli-bridge] serving {cfg.title} on http://{args.host}:{args.port}", flush=True)
    print(f"[cli-bridge] operations: {', '.join(op['path'] for op in state.operations)}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="CLI → OpenAPI HTTP bridge")
    sub = parser.add_subparsers(dest="cmd", required=True)

    gen = sub.add_parser("generate", help="emit OpenAPI spec")
    gen.add_argument("--config", required=True)
    gen.add_argument("-o", "--output")
    gen.set_defaults(func=cmd_generate)

    serve = sub.add_parser("serve", help="run HTTP bridge")
    serve.add_argument("--config", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=3860)
    serve.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())