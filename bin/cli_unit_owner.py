#!/usr/bin/env python3
"""Look-only owner check for mcp-cli-server@NAME (user vs system).

Live rule: CLI MCPs run as the linger **user** unit `mcp-cli-server@<name>`.
A matching **system** instance must stay disabled so the two do not fight
for the same bridge/MCP ports (acpx-cli class: bridge :3870 / MCP :8826).

This module only runs `systemctl is-active` / `is-enabled` / `list-units` /
`list-unit-files`. It never starts, stops, enables, or disables a unit.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable

# Port-holding / would-start-on-boot. Failed is not listening; enabled still
# counts because Restart=always will bring the unit back.
ACTIVE_STATES = frozenset({"active", "activating", "reloading"})
ENABLED_STATES = frozenset(
    {"enabled", "enabled-runtime", "linked", "linked-runtime"}
)

UNIT_PREFIX = "mcp-cli-server@"
_UNIT_NAME = re.compile(r"mcp-cli-server@(\S+)")

RunFn = Callable[[list[str]], str]


def _default_run(cmd: list[str], timeout: int = 5) -> str:
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    out = (r.stdout or "").strip()
    if out:
        return out
    err = (r.stderr or "").strip()
    return err.splitlines()[0] if err else ""


def first_token(raw: str) -> str:
    """Take the first whitespace-separated token from a systemctl verb."""
    if not raw:
        return "unknown"
    return raw.strip().split()[0].lower()


def in_play(active: str, enabled: str) -> bool:
    return first_token(active) in ACTIVE_STATES or first_token(enabled) in ENABLED_STATES


def names_from_manifest(manifest: dict) -> list[str]:
    names: list[str] = []
    for name, cfg in manifest.items():
        if not isinstance(cfg, dict):
            continue
        if cfg.get("cli_config") or cfg.get("cli_port"):
            names.append(str(name))
    return names


def names_from_systemctl_list(text: str) -> set[str]:
    found: set[str] = set()
    for match in _UNIT_NAME.finditer(text or ""):
        inst = match.group(1)
        if inst.endswith(".service"):
            inst = inst[: -len(".service")]
        if not inst or inst == "*":
            continue
        found.add(inst)
    return found


def conflict_message(
    name: str,
    user_active: str,
    user_enabled: str,
    system_active: str,
    system_enabled: str,
) -> str | None:
    if not in_play(user_active, user_enabled) or not in_play(system_active, system_enabled):
        return None
    ua, ue = first_token(user_active), first_token(user_enabled)
    sa, se = first_token(system_active), first_token(system_enabled)
    return (
        f"{UNIT_PREFIX}{name}: user and system instances both in play "
        f"(user {ua}/{ue}, system {sa}/{se}) — prefer linger user unit; "
        "leave the system instance disabled"
    )


def query_verb(run: RunFn, scope: str, verb: str, unit: str) -> str:
    cmd = ["systemctl"]
    if scope == "user":
        cmd.append("--user")
    # Look-only verbs. Never start/stop/enable/disable.
    cmd += [verb, "--no-pager", "--", unit]
    return first_token(run(cmd))


def discover_listed_names(run: RunFn) -> set[str]:
    names: set[str] = set()
    list_cmds = (
        [
            "systemctl",
            "--user",
            "list-units",
            "--all",
            "--type=service",
            "--no-legend",
            "--no-pager",
            f"{UNIT_PREFIX}*",
        ],
        [
            "systemctl",
            "list-units",
            "--all",
            "--type=service",
            "--no-legend",
            "--no-pager",
            f"{UNIT_PREFIX}*",
        ],
        [
            "systemctl",
            "--user",
            "list-unit-files",
            "--no-legend",
            "--no-pager",
            f"{UNIT_PREFIX}*",
        ],
        [
            "systemctl",
            "list-unit-files",
            "--no-legend",
            "--no-pager",
            f"{UNIT_PREFIX}*",
        ],
    )
    for cmd in list_cmds:
        names |= names_from_systemctl_list(run(cmd))
    return names


def collect_names(manifest: dict | None, run: RunFn) -> list[str]:
    names = set(names_from_manifest(manifest or {}))
    names |= discover_listed_names(run)
    return sorted(names)


def check_conflicts(
    names: Iterable[str],
    run: RunFn,
) -> list[str]:
    warnings: list[str] = []
    for name in names:
        unit = f"{UNIT_PREFIX}{name}"
        user_active = query_verb(run, "user", "is-active", unit)
        user_enabled = query_verb(run, "user", "is-enabled", unit)
        system_active = query_verb(run, "system", "is-active", unit)
        system_enabled = query_verb(run, "system", "is-enabled", unit)
        msg = conflict_message(
            name, user_active, user_enabled, system_active, system_enabled
        )
        if msg:
            warnings.append(msg)
    return warnings


def load_manifest(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def format_report(names: list[str], warnings: list[str]) -> list[str]:
    if not names:
        return ["ok: no mcp-cli-server@ instances to check"]
    if not warnings:
        return [f"ok: no user+system overlap for {len(names)} mcp-cli-server@ name(s)"]
    lines = [f"⚠ {msg}" for msg in warnings]
    lines.append(
        f"{len(warnings)} dual-owner warning(s) — look-only; no units started or stopped"
    )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--manifest",
        default="",
        help="servers.json (or example) used to discover CLI names",
    )
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    manifest_path = Path(args.manifest) if args.manifest else root / "servers.json"
    if not manifest_path.is_file():
        fallback = root / "servers.example.json"
        if fallback.is_file():
            manifest_path = fallback
    manifest = load_manifest(manifest_path)
    names = collect_names(manifest, _default_run)
    warnings = check_conflicts(names, _default_run)
    for line in format_report(names, warnings):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
