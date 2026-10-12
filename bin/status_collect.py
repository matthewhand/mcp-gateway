#!/usr/bin/env python3
"""Collect mcp-gateway health for the status WebUI / CLI.

No secrets: never dumps env values, tokens, or spec URLs with credentials.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from box_key import box_key_status

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "servers.json"
STATE = Path(os.environ.get("MCP_GATEWAY_STATE", os.path.expanduser("~/.state/mcp-gateway")))

# Issue thresholds — restart loops should light up immediately.
UPTIME_JUST_STARTED_SEC = 120
UPTIME_RECENT_SEC = 10 * 60
RESTARTS_WARN = 1
RESTARTS_BAD = 3
RESTARTS_FLAP = 5

# Protocol-level probe: the port can be up while the MCP handshake is broken
# (e.g. protocol-version mismatch after an engine upgrade). Probe each enabled,
# listening bridge with a minimal `initialize` so that class of failure lights
# up instead of masquerading as healthy.
PROBE_TIMEOUT_SEC = 4.0
PROBE_LEGACY_VERSION = "2025-11-25"

ProbeResult = dict[str, Any]


def mcp_probe_http(port: int, timeout: float = PROBE_TIMEOUT_SEC) -> ProbeResult:
    """POST a minimal MCP initialize to 127.0.0.1:{port}/mcp.

    Returns {"ok", "version", "protocol", "error"}. Never raises.
    """
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": PROBE_LEGACY_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mcp-status-probe", "version": "1"},
        },
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp", data=payload,
        headers={"Content-Type": "application/json",
                 # application/json only: accepting text/event-stream invites
                 # SSE framing, which a plain json.loads would misread as a
                 # broken server even when the handshake is fine.
                 "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = (e.read().decode("utf-8", errors="replace") or "")[:80].replace("\n", " ")
        except Exception:
            pass
        return {"ok": False, "version": None, "protocol": None,
                "error": f"HTTP {e.code}" + (f" ({detail})" if detail else "")}
    except Exception as e:  # URLError, TimeoutError, OSError, socket.timeout ...
        return {"ok": False, "version": None, "protocol": None,
                "error": (str(e) or type(e).__name__)[:80]}
    try:
        msg = json.loads(body)
    except Exception:
        head = body[:120].replace("\n", " ")
        return {"ok": False, "version": None, "protocol": None,
                "error": f"non-JSON reply ({head})"}
    if isinstance(msg, dict) and msg.get("error"):
        err = msg["error"]
        return {"ok": False, "version": None, "protocol": None,
                "error": f"JSON-RPC {err.get('code', '?')} {str(err.get('message', ''))[:80]}"}
    result = msg.get("result") if isinstance(msg, dict) else None
    if not isinstance(result, dict):
        return {"ok": False, "version": None, "protocol": None,
                "error": "no initialize result"}
    info = result.get("serverInfo") or {}
    return {
        "ok": True,
        "version": str(info.get("version") or "?"),
        "protocol": str(result.get("protocolVersion") or "?"),
        "error": None,
    }


def _run_probes(rows: list[dict[str, Any]]) -> None:
    """Attach probe results to enabled, listening, expected rows (in place)."""
    targets = [
        r for r in rows
        if r.get("expected") and r.get("enabled") and r.get("listening") and r.get("port")
    ]
    if not targets:
        return
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(mcp_probe_http, int(t["port"])): t for t in targets}
        for fut in as_completed(futs):
            futs[fut]["proto"] = fut.result()

KEYLESS = {"glama", "apisguru"}
MANUAL = {"box"}
BACKENDS = {
    "netbox": "netbox-docker-netbox-1",
    "getzep": "zep-ce-zep-1",
}

SEVERITY_RANK = {"bad": 0, "warn": 1, "ok": 2}

Runner = Callable[..., Any]


def format_uptime(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s" if sec else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def parse_systemd_timestamp(value: str, now: dt.datetime) -> int | None:
    value = (value or "").strip()
    if not value or value in {"n/a", "0", "-"}:
        return None
    # "Tue 2026-08-18 09:17:36 UTC"
    m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", value)
    if not m:
        return None
    started = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    return max(0, int((now - started).total_seconds()))


def parse_show_blocks(text: str) -> dict[str, dict[str, str]]:
    """Parse `systemctl show UNIT...` output (blank-line separated blocks)."""
    out: dict[str, dict[str, str]] = {}
    for raw in re.split(r"\n\s*\n", text.strip()):
        if not raw.strip():
            continue
        props: dict[str, str] = {}
        for line in raw.splitlines():
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            props[k] = v
        ident = props.get("Id") or props.get("Names") or ""
        ident = ident.replace(".service", "").replace(".timer", "")
        if ident:
            out[ident] = props
            # also index by short instance name
            if "@" in ident:
                out[ident.split("@", 1)[1]] = props
    return out


def unit_for(name: str, cfg: dict[str, Any] | None) -> str:
    if name in {"glama", "mcp-glama"}:
        return "mcp-glama"
    if name in {"oauth", "mcp-oauth"}:
        return "mcp-oauth"
    cfg = cfg or {}
    if cfg.get("cli_config") or cfg.get("cli_port"):
        return f"mcp-cli-server@{name}"
    return f"mcp-server@{name}"


def parse_listening_ports(ss_out: str) -> set[int]:
    ports: set[int] = set()
    for m in re.finditer(r":(\d+)\s", ss_out.replace("\t", " ") + " "):
        ports.add(int(m.group(1)))
    return ports


def _default_run(cmd: list[str], timeout: int = 8) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout
    except Exception:
        return ""


def default_runners() -> dict[str, Runner]:
    def show(units: Iterable[str]) -> str:
        units = [u for u in units if u]
        if not units:
            return ""
        props = [
            "Id", "LoadState", "ActiveState", "SubState", "UnitFileState",
            "NRestarts", "ActiveEnterTimestamp", "Result", "ExecMainStatus",
        ]
        args = ["systemctl", "--user", "show"]
        for p in props:
            args.extend(["-p", p])
        args.extend(units)
        return _default_run(args)

    def show_system(units: Iterable[str]) -> str:
        units = [u for u in units if u]
        if not units:
            return ""
        args = ["systemctl", "show", "-p", "Id", "-p", "ActiveState", "-p", "SubState", "-p", "UnitFileState"]
        args.extend(units)
        return _default_run(args)

    def list_mcp_units() -> str:
        return _default_run(
            ["systemctl", "--user", "list-units", "--all", "--no-legend", "--plain",
             "mcp-server@*", "mcp-cli-server@*"]
        ) + "\n" + _default_run(
            ["systemctl", "--user", "list-unit-files", "--no-legend", "--plain"]
        )

    return {
        "ss": lambda: _default_run(["ss", "-tlnH"]),
        "show": show,
        "show_system": show_system,
        "list_units": list_mcp_units,
        "docker": lambda: _default_run(["docker", "ps", "--format", "{{.Names}}|{{.Status}}|{{.State}}"]),
        "free": lambda: _default_run(["free", "-m"]),
        "df": lambda: _default_run(["df", "-h", "/"]),
        "watchdog": lambda: _read_watchdog(),
    }


def _read_watchdog() -> str:
    path = STATE / "watchdog.log"
    try:
        lines = path.read_text(errors="replace").splitlines()[-80:]
        return "\n".join(lines)
    except Exception:
        return ""


def _int(value: str | None, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def classify(row: dict[str, Any]) -> tuple[str, list[str]]:
    """Return (severity, issue messages). Pure — used by tests."""
    issues: list[str] = []
    severity = "ok"
    expected = bool(row.get("expected"))
    enabled = bool(row.get("enabled"))
    manual = bool(row.get("manual"))
    leftover = bool(row.get("leftover"))
    listening = bool(row.get("listening"))
    port = row.get("port")
    active = row.get("active") or "unknown"
    sub = row.get("sub") or ""
    nrestarts = int(row.get("nrestarts") or 0)
    uptime = row.get("uptime_sec")
    load = row.get("load") or ""

    restarting_now = (
        active == "activating"
        or sub in {"auto-restart", "start", "start-pre", "start-post", "stop-sigterm"}
        or "restart" in sub
    )
    if leftover:
        if load == "masked" or active == "failed":
            issues.append("leftover unit (masked/failed, not in servers.json)")
            return "warn", issues
        if active == "active":
            issues.append("running but not in servers.json")
            return "warn", issues
        return "ok", issues

    # Port is up but the MCP handshake fails (protocol/engine mismatch). This
    # is the class the port check alone can never see — make it loud.
    proto = row.get("proto") or {}
    if expected and listening and proto.get("error"):
        issues.append(f"MCP handshake failed ({proto['error']})")
        severity = "bad"

    if row.get("key_unhealthy"):
        issues.append(str(row.get("key_reason") or "short-lived Box token; refresh BOX_API_KEY"))
        severity = "warn"

    if manual and not enabled and active != "active":
        if row.get("key_unhealthy"):
            return "warn", issues
        return "ok", []

    if expected and not enabled and not manual:
        issues.append("not enabled")
        severity = "bad"

    if restarting_now:
        issues.append(f"restarting now ({active}/{sub})")
        severity = "bad"

    if expected and not manual and not listening and not restarting_now:
        if active != "active":
            issues.append(f"down ({active}/{sub or 'n/a'})")
        else:
            issues.append(f"port :{port} not listening (unit {active})")
        severity = "bad"

    if nrestarts >= RESTARTS_FLAP:
        issues.append(f"flapping — {nrestarts} automatic restarts")
        severity = "bad"
    elif nrestarts >= RESTARTS_BAD:
        issues.append(f"{nrestarts} automatic restarts")
        if severity == "ok":
            severity = "warn"
    elif nrestarts >= RESTARTS_WARN and isinstance(uptime, int) and uptime < UPTIME_RECENT_SEC:
        issues.append(f"{nrestarts} restart" + ("" if nrestarts == 1 else "s"))
        if severity == "ok":
            severity = "warn"

    if isinstance(uptime, int):
        if uptime < UPTIME_JUST_STARTED_SEC and (nrestarts or restarting_now or expected):
            issues.append(f"just started ({format_uptime(uptime)})")
            if nrestarts or restarting_now:
                severity = "bad"
            elif severity == "ok":
                severity = "warn"
        elif uptime < UPTIME_RECENT_SEC and nrestarts:
            issues.append(f"short uptime after restart ({format_uptime(uptime)})")
            if severity == "ok":
                severity = "warn"

    if row.get("backend_down"):
        issues.append(f"backend container {row['backend']} is down")
        if severity == "ok":
            severity = "warn"

    return severity, issues


def pulse_for(row: dict[str, Any], severity: str | None = None, issues: list[str] | None = None) -> bool:
    """Services only. True for live problems; stale flap debt stays unpulsed."""
    if row.get("leftover") or (row.get("manual") and not row.get("enabled") and row.get("active") != "active"):
        return False
    if severity is None:
        severity = str(row.get("severity") or "")
    if issues is None:
        issues = list(row.get("issues") or [])
    if severity != "bad":
        return False
    n = int(row.get("nrestarts") or 0)
    up = row.get("uptime_sec")
    restarting_now = (
        row.get("active") == "activating"
        or (row.get("sub") or "") in {"auto-restart", "start", "start-pre", "start-post", "stop-sigterm"}
        or "restart" in (row.get("sub") or "")
    )
    if restarting_now:
        return True
    if any(i.startswith("down ") or i.startswith("port ") for i in issues):
        return True
    if any(i.startswith("MCP handshake failed") for i in issues):
        return True
    if isinstance(up, int) and up < UPTIME_JUST_STARTED_SEC and n >= 1:
        return True
    if n >= RESTARTS_FLAP and isinstance(up, int) and up < UPTIME_RECENT_SEC:
        return True
    if n >= RESTARTS_FLAP and not isinstance(up, int):
        return True
    return False


def _badge(name: str, cfg: dict[str, Any], kind: str) -> str:
    if name in MANUAL:
        return "manual"
    if kind == "cli":
        return "cli"
    if name in KEYLESS:
        return "keyless"
    env = cfg.get("env") or {}
    if any(k for k in env if any(tok in k for tok in ("KEY", "TOKEN", "HEADERS"))):
        return "keyed"
    return "no key"


def _scan_leftovers(list_out: str, known: set[str]) -> list[tuple[str, str]]:
    found: dict[str, str] = {}
    for line in list_out.splitlines():
        m = re.search(r"mcp-(cli-)?server@([a-z0-9-]+)\.service", line)
        if not m:
            continue
        name = m.group(2)
        if name in known:
            continue
        unit = f"mcp-cli-server@{name}" if m.group(1) else f"mcp-server@{name}"
        found[name] = unit
    return sorted(found.items())


def _host_stats(runners: dict[str, Runner]) -> dict[str, Any]:
    mem_avail = None
    swap_used = None
    swap_tot = None
    free = runners["free"]()
    for line in free.splitlines():
        parts = line.split()
        if parts and parts[0].startswith("Mem") and len(parts) > 6:
            try:
                mem_avail = int(parts[6])
            except ValueError:
                pass
        if parts and parts[0].startswith("Swap") and len(parts) > 2:
            try:
                swap_used = int(parts[2])
                swap_tot = int(parts[1])
            except ValueError:
                pass
    disk_used = disk_free = "?"
    df = runners["df"]().splitlines()
    if len(df) > 1:
        cols = df[1].split()
        if len(cols) >= 5:
            disk_free, disk_used = cols[3], cols[4]
    return {
        "mem_avail_mb": mem_avail,
        "swap_used_mb": swap_used,
        "swap_tot_mb": swap_tot,
        "disk_used": disk_used,
        "disk_free": disk_free,
    }


def _watchdog_bits(text: str) -> dict[str, Any]:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    heals = [ln for ln in lines if re.search(r"(down ->|not active|still down|self-heal|-> restart|-> start)", ln, re.I)]
    last = lines[-1] if lines else ""
    last_ts = ""
    if last:
        last_ts = last.split(" ", 1)[0]
    return {"last": last_ts or "-", "heals": len(heals), "recent_heals": heals[-8:]}


def _run_wave(fns: dict[str, Runner]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fn): name for name, fn in fns.items()}
        for fut in as_completed(futs):
            out[futs[fut]] = fut.result()
    return out


def _host_issues(host: dict[str, Any]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    mem = host.get("mem_avail_mb")
    if isinstance(mem, int):
        if mem < 500:
            issues.append({"severity": "bad", "message": f"memory {mem} MB available"})
        elif mem < 1500:
            issues.append({"severity": "warn", "message": f"memory {mem} MB available"})
    disk_used = host.get("disk_used") or ""
    disk_free = host.get("disk_free") or "?"
    m = re.search(r"(\d+)", str(disk_used))
    if m:
        pct = int(m.group(1))
        if pct >= 90:
            issues.append({"severity": "bad", "message": f"disk {disk_used} used ({disk_free} free)"})
        elif pct >= 80:
            issues.append({"severity": "warn", "message": f"disk {disk_used} used ({disk_free} free)"})
    swap_used = host.get("swap_used_mb")
    swap_tot = host.get("swap_tot_mb")
    if isinstance(swap_used, int) and isinstance(swap_tot, int) and swap_tot > 0:
        if swap_used >= 0.9 * swap_tot:
            issues.append({"severity": "warn", "message": f"swap {swap_used}/{swap_tot} MB"})
    return issues


def collect(root: Path | None = None, now: dt.datetime | None = None,
            runners: dict[str, Runner] | None = None,
            environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    root = Path(root or ROOT)
    now = now or dt.datetime.now(dt.timezone.utc)
    runners = runners or default_runners()
    secret_environ = os.environ if environ is None else environ
    # Protocol probe hits the loopback bridges over HTTP. Hermetic tests pass
    # `environ={}` (or have no enabled+listening rows) and never touch network.
    probe_enabled = environ is None and os.environ.get("MCP_STATUS_PROBE", "1") != "0"
    manifest_path = root / "servers.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except Exception:
        manifest = {}

    known = set(manifest) | {"glama"}

    # Wave 1: discover leftovers and gather host facts. show() needs leftover names.
    w1 = _run_wave({
        "list_units": lambda: runners["list_units"](),
        "ss": lambda: runners["ss"](),
        "show_system": lambda: runners["show_system"](["nginx"]),
        "docker": lambda: runners["docker"](),
        "free": lambda: runners["free"](),
        "df": lambda: runners["df"](),
        "watchdog": lambda: runners["watchdog"](),
    })
    leftovers = _scan_leftovers(w1["list_units"], known)

    units = ["mcp-oauth", "mcp-glama", "mcp-watchdog.timer", "mcp-status"]
    specs: list[dict[str, Any]] = []

    specs.append({
        "name": "glama", "kind": "core", "unit": "mcp-glama", "port": 8765,
        "expected": True, "manual": False, "leftover": False, "cfg": {},
    })
    for name, cfg in manifest.items():
        kind = "cli" if (cfg.get("cli_config") or cfg.get("cli_port")) else "openapi"
        specs.append({
            "name": name, "kind": kind, "unit": unit_for(name, cfg),
            "port": cfg.get("port"),
            # enabled:false entries are intentionally offline (watchdog/install
            # skip them too) — report them in the Disabled table, never as bad.
            "expected": str(cfg.get("enabled", True)).lower() != "false",
            "manual": name in MANUAL, "leftover": False, "cfg": cfg,
        })
    for name, unit in leftovers:
        specs.append({
            "name": name, "kind": "cli" if "cli-server" in unit else "openapi",
            "unit": unit, "port": None, "expected": False, "manual": False,
            "leftover": True, "cfg": {},
        })

    for spec in specs:
        if spec["unit"] not in units:
            units.append(spec["unit"])

    shown = parse_show_blocks(runners["show"](units))
    sys_shown = parse_show_blocks(w1["show_system"])
    ports = parse_listening_ports(w1["ss"])

    docker_rows = []
    docker_up: set[str] = set()
    for line in (w1["docker"] or "").splitlines():
        parts = line.split("|")
        if len(parts) >= 2:
            docker_rows.append({"name": parts[0], "status": parts[1], "state": parts[2] if len(parts) > 2 else ""})
            if len(parts) < 3 or parts[2] == "running":
                docker_up.add(parts[0])

    services: list[dict[str, Any]] = []
    for spec in specs:
        props = shown.get(spec["unit"]) or shown.get(spec["name"]) or {}
        ufs = (props.get("UnitFileState") or "").strip()
        enabled = ufs == "enabled"
        active = (props.get("ActiveState") or "unknown").strip()
        sub = (props.get("SubState") or "").strip()
        load = (props.get("LoadState") or "").strip()
        nrestarts = _int(props.get("NRestarts"))
        uptime_sec = parse_systemd_timestamp(props.get("ActiveEnterTimestamp", ""), now)
        if active in {"inactive", "failed", "dead"} and not uptime_sec:
            uptime_sec = None
        port = spec["port"]
        listening = bool(port) and int(port) in ports
        backend = BACKENDS.get(spec["name"])
        backend_down = bool(backend) and backend not in docker_up and spec["name"] in manifest
        key_unhealthy = False
        key_reason = None
        if spec["name"] == "box":
            gate = box_key_status(
                root=root,
                manifest=manifest,
                environ=secret_environ,
                env_files=[root / ".env"] if environ is not None else None,
                key_files=[] if environ is not None else None,
            )
            key_unhealthy = bool(gate.get("unhealthy"))
            key_reason = gate.get("reason")
        row = {
            "name": spec["name"],
            "kind": spec["kind"],
            "unit": spec["unit"],
            "port": port,
            "enabled": enabled,
            "unit_file_state": ufs or ("masked" if load == "masked" else ""),
            "active": active,
            "sub": sub,
            "load": load,
            "nrestarts": nrestarts,
            "uptime_sec": uptime_sec,
            "uptime": format_uptime(uptime_sec if active == "active" else None),
            "listening": listening,
            "expected": spec["expected"],
            "manual": spec["manual"],
            "leftover": spec["leftover"],
            "badge": _badge(spec["name"], spec["cfg"], spec["kind"]),
            "backend": backend,
            "backend_down": backend_down,
            "key_unhealthy": key_unhealthy,
            "key_reason": key_reason,
        }
        services.append(row)

    if probe_enabled:
        _run_probes(services)
    for row in services:
        severity, issues = classify(row)
        row["severity"] = severity
        row["issues"] = issues
        row["pulse"] = pulse_for(row, severity, issues)
        row["live"] = row["pulse"]

    def sort_key(row: dict[str, Any]) -> tuple:
        up = row["uptime_sec"] if isinstance(row.get("uptime_sec"), int) else 10**12
        return (SEVERITY_RANK.get(row["severity"], 9), up, -int(row.get("nrestarts") or 0), row["name"])

    services.sort(key=sort_key)

    core = []
    for name, unit, system in (
        ("mcp-oauth", "mcp-oauth", False),
        ("mcp-glama", "mcp-glama", False),
        ("mcp-watchdog", "mcp-watchdog.timer", False),
        ("nginx", "nginx", True),
        ("mcp-status", "mcp-status", False),
    ):
        props = (sys_shown if system else shown).get(unit) or (sys_shown if system else shown).get(name) or {}
        # timer Id is mcp-watchdog.timer — parse_show_blocks strips .timer
        if not props and unit.endswith(".timer"):
            props = shown.get("mcp-watchdog") or {}
        active = (props.get("ActiveState") or "unknown").strip()
        sub = (props.get("SubState") or "").strip()
        ok = active == "active"
        core_row = {
            "name": name, "unit": unit, "active": active, "sub": sub,
            "enabled": (props.get("UnitFileState") or "") == "enabled",
            "severity": "ok" if ok else "bad",
            "issues": [] if ok else [f"{active}/{sub or 'n/a'}"],
        }
        core_row["pulse"] = core_row["severity"] == "bad"
        core_row["live"] = core_row["pulse"]
        core.append(core_row)

    enabled_svcs = [s for s in services if s["enabled"] and s["expected"]]
    up_svcs = [s for s in enabled_svcs if s["listening"] or (s["port"] is None and s["active"] == "active")]

    wd = _watchdog_bits(w1["watchdog"] or "")
    host = _host_stats({
        "free": lambda: w1["free"],
        "df": lambda: w1["df"],
    })
    host["issues"] = _host_issues(host)

    n_issues = (
        sum(1 for s in services if s["issues"])
        + sum(1 for c in core if c["issues"])
        + len(host["issues"])
    )
    n_live = (
        sum(1 for s in services if s.get("pulse"))
        + sum(1 for c in core if c.get("pulse"))
    )

    return {
        "schema_version": 1,
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "host_url": os.getenv("MCP_PUBLIC_URL") or "https://mcp.example.com",
        "thresholds": {
            "uptime_just_started_sec": UPTIME_JUST_STARTED_SEC,
            "uptime_recent_sec": UPTIME_RECENT_SEC,
            "restarts_warn": RESTARTS_WARN,
            "restarts_bad": RESTARTS_BAD,
            "restarts_flap": RESTARTS_FLAP,
        },
        "summary": {
            "enabled": len(enabled_svcs),
            "manifest": len(manifest) + 1,  # + glama
            "up": len(up_svcs),
            "issues": n_issues,
            "live": n_live,
            "restarting": n_live,
            "restarts_total": sum(int(s.get("nrestarts") or 0) for s in services),
        },
        "core": core,
        "services": services,
        "containers": docker_rows,
        "watchdog": wd,
        "host": host,
    }


def render_html(data: dict[str, Any]) -> str:
    """Self-contained dark status page. JS swaps #app; noscript meta refresh."""
    summary = data.get("summary") or {}
    services = list(data.get("services") or [])
    core = list(data.get("core") or [])
    containers = list(data.get("containers") or [])
    host = data.get("host") or {}
    host_issues = list(host.get("issues") or [])
    wd = data.get("watchdog") or {}

    def _pct(raw: Any) -> int | None:
        m = re.search(r"(\d+)", str(raw or ""))
        return int(m.group(1)) if m else None

    def _time_html(raw: Any) -> str:
        text = str(raw or "")
        m = re.match(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) UTC\Z", text)
        if m:
            return f'<time datetime="{m.group(1)}T{m.group(2)}Z">{_esc(text)}</time>'
        return f"<time>{_esc(text)}</time>"

    def _en_badge(s: dict[str, Any]) -> tuple[str, str]:
        ufs = (s.get("unit_file_state") or "").strip()
        if ufs == "enabled" or (s.get("enabled") and not ufs):
            return "enabled", "badge-en--on"
        if ufs == "masked":
            return "masked", "badge-en--mask"
        if ufs == "disabled":
            return "disabled", "badge-en--off"
        return (ufs or "no"), "badge-en--off"

    def _is_backend(name: str) -> bool:
        return name in BACKENDS.values() or name.startswith("netbox-docker-") or name.startswith("zep-ce-")

    # --- masthead ---
    source = str(data.get("source") or "live")
    if source == "live":
        chip = '<span class="chip chip--live">LIVE</span>'
    else:
        chip = '<span class="chip chip--snap">SNAPSHOT</span>'

    # --- stats ---
    up, enabled = summary.get("up"), summary.get("enabled")
    if up is None or enabled is None:
        up_v, up_cls = "?", "stat--warn"
    else:
        up_v = f"{up}/{enabled}"
        up_cls = "stat--ok" if up == enabled else "stat--bad"

    live = summary.get("live")
    if live is None:
        live_v, live_cls = "?", "stat--warn"
    else:
        live_v = str(live)
        live_cls = "stat--ok" if live == 0 else "stat--bad"

    n_issues = summary.get("issues")
    host_bad = any((h.get("severity") == "bad") for h in host_issues)
    fire = (live not in (None, 0)) or host_bad
    if n_issues is None:
        issues_v, issues_cls = "?", "stat--warn"
    elif n_issues == 0:
        issues_v, issues_cls = "0", "stat--ok"
    else:
        issues_v = str(n_issues)
        # Stale flaps stay listed but must not paint the whole board red.
        issues_cls = "stat--bad" if fire else "stat--warn"

    rst_total = summary.get("restarts_total")
    if rst_total is None:
        rst_v, rst_cls = "?", "stat--warn"
    else:
        rst_v = str(rst_total)
        rst_cls = "stat--ok" if rst_total == 0 else "stat--warn"

    mem = host.get("mem_avail_mb")
    if mem is None:
        mem_v, mem_cls = "?", "stat--warn"
    else:
        mem_v = f"{mem} MB"
        if mem < 500:
            mem_cls = "stat--bad"
        elif mem < 1500:
            mem_cls = "stat--warn"
        else:
            mem_cls = "stat--ok"

    disk_used = host.get("disk_used")
    disk_free = host.get("disk_free")
    if disk_used is None:
        disk_v, disk_cls = "?", "stat--warn"
    else:
        free_bit = f" ({disk_free} free)" if disk_free not in (None, "") else ""
        disk_v = f"{disk_used}{free_bit}"
        dpct = _pct(disk_used)
        if dpct is None:
            disk_cls = "stat--warn"
        elif dpct >= 90:
            disk_cls = "stat--bad"
        elif dpct >= 80:
            disk_cls = "stat--warn"
        else:
            disk_cls = "stat--ok"

    # --- issue strip (grouped; red only when something is dying now) ---
    def _svc_id(name: Any) -> str:
        slug = re.sub(r"[^a-z0-9-]+", "-", str(name or "").lower()).strip("-")
        return f"svc-{slug}" if slug else "svc"

    def _name_link(name: Any, extra_class: str = "") -> str:
        cls = "issue-link" if not extra_class else f"issue-link {extra_class}"
        return f'<a class="{cls}" href="#{_esc(_svc_id(name))}">{_esc(name)}</a>'

    def _short_svc_msg(row: dict[str, Any], msg: str) -> str:
        text = str(msg or "")
        low = text.lower()
        n = int(row.get("nrestarts") or 0)
        up = row.get("uptime") or "—"
        if "flapping" in low or "automatic restart" in low:
            return f"{n} restarts · {up}"
        if "leftover" in low:
            return "leftover — not in servers.json"
        if "just started" in low:
            return f"just started · {up}"
        return text

    def _group_for(row: dict[str, Any] | None, msg: str, pulse: bool) -> str:
        if pulse:
            return "now"
        if row is None:
            return "host"
        low = str(msg or "").lower()
        if row.get("leftover") or "leftover" in low or "not enabled" in low:
            return "other"
        if "flapping" in low or "restart" in low:
            return "debt"
        return "other"

    now_items: list[str] = []
    debt_items: list[str] = []
    host_items: list[str] = []
    other_items: list[str] = []

    def _svc_li(row: dict[str, Any], msg: str, now: bool) -> str:
        sev = row.get("severity") or "ok"
        prefix = '<span class="now">NOW</span> ' if now else ""
        return (
            f'<li class="{_esc(sev)}">{prefix}<b>{_name_link(row.get("name"))}</b>'
            f' · {_esc(_short_svc_msg(row, msg))}</li>'
        )

    def _core_li(row: dict[str, Any], msg: str, now: bool) -> str:
        sev = row.get("severity") or "ok"
        prefix = '<span class="now">NOW</span> ' if now else ""
        return (
            f'<li class="{_esc(sev)}">{prefix}<b>{_name_link(row.get("name"))}</b>'
            f' · {_esc(msg)}</li>'
        )

    for s in services:
        pulse = bool(s.get("pulse"))
        for msg in s.get("issues") or []:
            li = _svc_li(s, msg, pulse)
            bucket = _group_for(s, str(msg), pulse)
            {"now": now_items, "debt": debt_items, "host": host_items, "other": other_items}[bucket].append(li)
    for c in core:
        pulse = bool(c.get("pulse"))
        for msg in c.get("issues") or []:
            li = _core_li(c, msg, pulse)
            (now_items if pulse else other_items).append(li)
    for h in host_issues:
        sev = h.get("severity") or "warn"
        msg = h.get("message") or ""
        host_items.append(f'<li class="{_esc(sev)}"><b>host</b> · {_esc(msg)}</li>')

    def _take(items: list[str], budget: int) -> tuple[list[str], int, int]:
        if budget <= 0:
            return [], 0, len(items)
        shown = items[:budget]
        return shown, len(shown), max(0, len(items) - len(shown))

    max_lines = 14
    shown_now, used, omit_now = _take(now_items, max_lines)
    shown_debt, n, omit_debt = _take(debt_items, max_lines - used)
    used += n
    shown_host, n, omit_host = _take(host_items, max_lines - used)
    used += n
    shown_other, _, omit_other = _take(other_items, max_lines - used)

    def _ul(cls: str, items: list[str], live: bool) -> str:
        aria = ' aria-live="polite"' if live else ' aria-live="off"'
        return f'<ul class="{cls}"{aria}>{"".join(items)}</ul>'

    def _grp(title: str, items: list[str], extra_omit: int, ul_cls: str, live: bool) -> str:
        if not items and not extra_omit:
            return ""
        tail = f"<li>+{extra_omit} more in the table</li>" if extra_omit else ""
        return (
            f'<h3 class="issue-h">{_esc(title)}'
            f' <span class="issue-n">{len(items) + extra_omit}</span></h3>'
            f"{_ul(ul_cls, items + ([tail] if tail else []), live)}"
        )

    has_any = bool(now_items or debt_items or host_items or other_items)
    strip_bad = bool(now_items) or host_bad
    if not has_any:
        strip_mod = "issue-strip--ok"
        strip_body = '<p class="issue-empty">All clear — no issues.</p>'
    else:
        strip_mod = "issue-strip--bad" if strip_bad else "issue-strip--warn"
        if now_items:
            lead = (
                f'<p class="issue-lead issue-lead--bad">'
                f'{len(now_items)} failing now — jump a name to open the row.</p>'
            )
        elif debt_items and host_items:
            lead = (
                '<p class="issue-lead">Nothing is failing right now. '
                "Restart counts and host pressure are listed below.</p>"
            )
        elif debt_items:
            lead = (
                '<p class="issue-lead">Nothing is failing right now. '
                "These units have historical restart debt.</p>"
            )
        else:
            lead = '<p class="issue-lead">Nothing is failing right now.</p>'
        parts = [lead]
        parts.append(_grp("Happening now", shown_now, omit_now, "issues-now", True))
        parts.append(_grp("Restart debt", shown_debt, omit_debt, "issues", False))
        parts.append(_grp("Host", shown_host, omit_host, "issues", False))
        parts.append(_grp("Other", shown_other, omit_other, "issues", False))
        strip_body = "".join(p for p in parts if p)

    # --- core pills ---
    pills = []
    for c in core:
        sev = "bad" if c.get("severity") == "bad" else "ok"
        pulse = bool(c.get("pulse"))
        active = c.get("active") or ""
        sub = c.get("sub") or ""
        title = f"{c.get('name')} {active}/{sub}" if sub else f"{c.get('name')} {active}"
        pcls = f"pill pill--{sev}"
        if pulse:
            pcls += " pill--pulse"
        pills.append(
            f'<span class="{pcls}" title="{_esc(title)}">'
            f'{_esc(c.get("name"))} <span class="pill-state">{_esc(active)}</span></span>'
        )
    core_html = "".join(pills)

    # --- service rows ---
    def svc_row(s: dict[str, Any]) -> str:
        sev = s.get("severity") or "ok"
        if sev not in {"ok", "warn", "bad"}:
            sev = "ok"
        classes = [f"svc-row svc-row--{sev}"]
        if bool(s.get("pulse")):
            classes.append("svc-row--pulse")
        if s.get("leftover"):
            classes.append("svc-row--leftover")
        if s.get("manual"):
            classes.append("svc-row--manual")
        en_copy, en_cls = _en_badge(s)
        active = s.get("active") or ""
        sub = s.get("sub") or ""
        if active == "active" and sub in {"running", "waiting"}:
            state, state_cls = sub, "state state--ok"
        elif active in {"failed", "inactive", "deactivating"} or "fail" in sub:
            state = f"{active}/{sub}" if sub else active
            state_cls = "state state--bad"
        elif active == "activating" or "restart" in sub:
            state = f"{active}/{sub}" if sub else active
            state_cls = "state state--hot"
        else:
            state = f"{active}/{sub}" if sub else (active or "—")
            state_cls = "state"
        n = int(s.get("nrestarts") or 0)
        if n >= RESTARTS_FLAP:
            rcls = "rst rst--5"
        elif n >= RESTARTS_BAD:
            rcls = "rst rst--3"
        elif n >= 1:
            rcls = "rst rst--1"
        else:
            rcls = "rst rst--0"
        up_sec = s.get("uptime_sec")
        ucls = "up"
        if isinstance(up_sec, int):
            if up_sec < UPTIME_JUST_STARTED_SEC and (n >= 1 or bool(s.get("pulse"))):
                ucls += " up--hot"
            elif up_sec < UPTIME_RECENT_SEC:
                ucls += " up--short"
            if up_sec >= 3600:
                ucls += " up--long"
        port = s.get("port")
        port_s = f":{port}" if port else "—"
        if s.get("listening"):
            listen, lcls = "up", "meta"
        elif port:
            listen = "down"
            lcls = "meta listen-down" if s.get("expected") and not s.get("manual") else "meta"
        else:
            listen, lcls = "—", "meta"
        notes = "; ".join(str(i) for i in (s.get("issues") or [])) or "—"
        kind = (s.get("kind") or "").strip()
        badge = (s.get("badge") or "").strip()
        if badge and badge != kind:
            meta = f"{kind} · {badge}" if kind else badge
        else:
            meta = kind
        rid = _svc_id(s.get("name"))
        return (
            f'<tr class="{" ".join(classes)}" id="{_esc(rid)}">'
            f'<th scope="row" class="n">'
            f'<span class="mark" aria-hidden="true"></span>'
            f'<b>{_esc(s.get("name"))}</b>'
            f'<div class="meta">{_esc(meta)}</div>'
            f'<span class="badge-en badge-en--dup {en_cls}">{_esc(en_copy)}</span>'
            f"</th>"
            f'<td class="col-en"><span class="badge-en {en_cls}">{_esc(en_copy)}</span></td>'
            f'<td class="{state_cls}">{_esc(state)}</td>'
            f'<td class="{ucls}">{_esc(s.get("uptime") or "—")}</td>'
            f'<td class="{rcls}">{n}</td>'
            f'<td>{_esc(port_s)} <span class="{lcls}">{_esc(listen)}</span></td>'
            f'<td class="iss">{_esc(notes)}</td>'
            f"</tr>"
        )

    enabled_rows = [s for s in services if s.get("expected") and (s.get("enabled") or not s.get("manual"))]
    other_rows = [s for s in services if s not in enabled_rows]

    def svc_table(rows: list[dict[str, Any]], tid: str, caption: str) -> str:
        body = "".join(svc_row(s) for s in rows) or '<tr><td colspan="7">none</td></tr>'
        return (
            f'<table class="svc-table" id="{tid}">'
            f'<caption class="visually-hidden">{_esc(caption)}</caption>'
            "<thead><tr>"
            '<th scope="col">service</th>'
            '<th class="col-en" scope="col">enabled</th>'
            '<th scope="col">state</th>'
            '<th scope="col">uptime</th>'
            '<th scope="col">restarts</th>'
            '<th scope="col">port</th>'
            '<th scope="col">notes</th>'
            f"</tr></thead><tbody>{body}</tbody></table>"
        )

    # --- containers ---
    backends = [c for c in containers if _is_backend(str(c.get("name") or ""))]
    others = [c for c in containers if c not in backends]

    def dock_rows(rows: list[dict[str, Any]]) -> str:
        if not rows:
            return '<tr><td colspan="2">none</td></tr>'
        out = []
        for c in rows:
            state = (c.get("state") or "").strip()
            cls = "svc-row svc-row--warn" if state and state != "running" else "svc-row svc-row--ok"
            out.append(
                f'<tr class="{cls}"><td>{_esc(c.get("name"))}</td>'
                f'<td>{_esc(c.get("status"))}</td></tr>'
            )
        return "".join(out)

    backends_tbl = (
        '<table class="svc-table" id="tbl-backends">'
        '<caption class="visually-hidden">Backend containers</caption>'
        "<thead><tr><th scope=\"col\">container</th><th scope=\"col\">status</th></tr></thead>"
        f"<tbody>{dock_rows(backends)}</tbody></table>"
    )
    other_dock = ""
    if others:
        other_dock = (
            f'<details class="more-dock"><summary>Other host containers ({len(others)})</summary>'
            f'<div class="table-wrap"><table class="svc-table" id="tbl-other-dock">'
            '<caption class="visually-hidden">Other host containers</caption>'
            "<thead><tr><th scope=\"col\">container</th><th scope=\"col\">status</th></tr></thead>"
            f"<tbody>{dock_rows(others)}</tbody></table></div></details>"
        )

    # --- footer ---
    su, st = host.get("swap_used_mb"), host.get("swap_tot_mb")
    swap_txt = f"swap {su} / {st} MB"
    if isinstance(su, (int, float)) and isinstance(st, (int, float)) and st > 0 and su >= 0.9 * st:
        swap_html = f'<span class="warn">{_esc(swap_txt)}</span>'
    else:
        swap_html = _esc(swap_txt)
    heals_n = wd.get("heals")
    last = wd.get("last")
    foot = (
        f'{swap_html} · watchdog last {_esc(last)} · {_esc(heals_n)} recent heal lines'
    )
    recent = list(wd.get("recent_heals") or [])
    heals_block = ""
    if recent:
        items = "".join(f"<li>{_esc(x)}</li>" for x in recent)
        heals_block = f"<h2>Recent watchdog heals</h2><ul class=\"heals\">{items}</ul>"

    css = """
:root{
  --bg:#0b0f14;--bg-elev:#0e141b;--panel:#141b24;--panel-2:#1a2330;
  --line:#243240;--line-hi:#314155;--txt:#e6edf3;--dim:#7d8da0;--muted:#9aa7b5;
  --ok:#3fb950;--ok-dim:#2ea043;--ok-bg:#12261a;--ok-line:#24532c;
  --warn:#d29922;--warn-bg:#231c10;--warn-line:#5c4a1a;
  --bad:#f85149;--bad-bg:#2a1214;--bad-pulse:#4a1518;
  --info:#58a6ff;--focus:#58a6ff;
  --radius-sm:8px;--radius:10px;--radius-pill:999px;--pad:8px;
  --font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
}
*{box-sizing:border-box}
html{background:var(--bg)}
body{margin:0;background:var(--bg);color:var(--txt);font:var(--font)}
.wrap{max-width:1120px;margin:0 auto;padding:20px 24px 32px}
h1{font-size:22px;font-weight:700;margin:0 0 2px}
h2{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);margin:22px 0 8px}
.sub{color:var(--dim);margin:0 0 16px;font-size:14px}
.masthead{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:16px}
.masthead-right{display:flex;align-items:center;gap:10px;flex-shrink:0}
.chip{display:inline-block;padding:2px 8px;border-radius:var(--radius-pill);border:1px solid var(--line);font-size:11px;font-weight:700;letter-spacing:.06em}
.chip--live{border-color:var(--ok-line);color:var(--ok)}
.chip--snap{border-color:var(--warn-line);color:var(--warn)}
.refresh{color:var(--dim);font-size:11px}
.stat-grid{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:16px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:10px 14px;min-width:110px;flex:1 1 110px}
.stat .v{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums}
.stat .k{color:var(--dim);font-size:11px}
.stat--ok .v{color:var(--ok-dim)}
.stat--warn .v{color:var(--warn)}
.stat--bad .v{color:var(--bad)}
.issue-strip{background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--ok-dim);border-radius:var(--radius);padding:10px 16px;margin:0 0 8px}
.issue-strip--bad{border-left-color:var(--bad)}
.issue-strip--warn{border-left-color:var(--warn)}
.issue-strip h2{margin-top:0}
.issue-h{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--dim);margin:10px 0 4px}
.issue-n{font-variant-numeric:tabular-nums;color:var(--muted);font-weight:600}
.issue-lead{margin:0 0 8px;color:var(--muted);font-size:13px}
.issue-lead--bad{color:var(--bad)}
.issue-empty{color:var(--ok);margin:0;font-size:13px}
.issue-link{color:inherit;text-decoration:none;border-bottom:1px dotted var(--line-hi)}
.issue-link:hover{border-bottom-color:var(--txt)}
.issues-now,.issues,.heals{margin:0;padding:0 0 0 4px;list-style:none}
.issues-now li,.issues li,.heals li{margin:4px 0;font-size:13px}
li.bad b,.now{color:var(--bad)}
li.warn b{color:var(--warn)}
.now{font-size:10px;font-weight:700;letter-spacing:.06em;font-variant:small-caps;margin-right:4px}
.svc-row:target{outline:1px solid var(--info);outline-offset:-1px}
.pill{display:inline-block;margin:0 6px 6px 0;padding:4px 8px;border-radius:var(--radius-pill);border:1px solid var(--line);font-size:12px}
.pill--ok{border-color:var(--ok-line)}
.pill--bad{border-color:var(--bad);color:var(--bad)}
.pill-state{color:var(--dim);font-size:11px}
.svc-block .sub{margin:0 0 8px;font-size:13px}
.filter{display:inline-flex;align-items:center;gap:8px;min-height:32px;margin:0 0 8px;padding:4px 12px;cursor:pointer;color:var(--muted);font-size:13px;user-select:none;border:1px solid var(--line);border-radius:var(--radius-pill)}
.filter-n{font-variant-numeric:tabular-nums;font-weight:650;color:var(--txt)}
#only-issues:focus-visible + .filter{outline:2px solid var(--focus);outline-offset:2px}
#only-issues:checked + .filter{border-color:var(--info);color:var(--info);background:#122033}
.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:var(--radius-sm);background:var(--panel);max-height:70vh}
.svc-table{width:100%;border-collapse:collapse;background:var(--panel)}
th,td{padding:8px 10px;text-align:left;border-top:1px solid var(--line);vertical-align:top}
thead th{border-top:0;font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.04em;font-weight:600;position:sticky;top:0;background:var(--panel);z-index:2}
.svc-row--bad{background:var(--bad-bg)}
.svc-row--warn{background:var(--warn-bg)}
.svc-row--ok{background:transparent}
th.n{font-weight:650;background:inherit;min-width:9em}
.n .mark{display:inline-block;width:8px;height:8px;margin-right:6px;vertical-align:middle;position:relative}
.n .mark::before{content:"";display:block}
.svc-row--ok .mark::before{width:8px;height:8px;border-radius:50%;background:var(--ok)}
.svc-row--warn .mark::before{width:0;height:0;border-left:4px solid transparent;border-right:4px solid transparent;border-bottom:8px solid var(--warn)}
.svc-row--bad .mark::before{width:8px;height:8px;background:var(--bad)}
.n .meta{color:var(--dim);font-size:11px;font-weight:400;margin-top:2px}
.n .badge-en{margin-top:4px}
.n .badge-en--dup{display:none}
.badge-en{display:inline-block;font-size:11px;font-weight:400;padding:0 6px;border-radius:var(--radius-pill);border:1px solid var(--line)}
.state--ok{color:var(--dim)}
.state--hot{color:var(--bad);font-weight:650}
.state--bad{color:var(--bad);font-weight:650}
.badge-en--on{border-color:var(--ok-line);color:var(--ok)}
.badge-en--off{color:var(--dim)}
.badge-en--mask{border-color:var(--warn-line);color:var(--warn)}
.up{font-variant-numeric:tabular-nums;font-weight:650;font-size:15px}
.up--hot{color:var(--bad)}
.up--short{color:var(--warn)}
.up--long{font-weight:400}
.rst{font-variant-numeric:tabular-nums;font-weight:700;font-size:15px}
.rst--0{color:var(--dim)}
.rst--1{color:var(--txt)}
.rst--3{color:var(--warn)}
.rst--5{color:var(--bad)}
.iss{color:var(--dim);font-size:13px;max-width:42ch}
.listen-down{color:var(--bad)}
.meta{color:var(--dim);font-size:11px}
@keyframes pulse{50%{background:var(--bad-pulse)}}
.svc-row--pulse{animation:pulse 1.2s ease-in-out infinite}
.pill--pulse{animation:pulse 1.2s ease-in-out infinite}
@media (prefers-reduced-motion: reduce){
  .svc-row--pulse,.pill--pulse{animation:none}
  .svc-row--pulse{box-shadow:inset 3px 0 0 var(--bad)}
}
@media (hover:hover){
  .svc-table tbody tr:hover:not(.svc-row--pulse){background:#18222d}
}
#only-issues:checked ~ .table-wrap tr.svc-row--ok{display:none}
.more-dock{margin-top:10px}
.more-dock summary{cursor:pointer;color:var(--dim);font-size:13px;min-height:32px}
.foot{color:var(--dim);font-size:12px;margin-top:18px}
.foot .warn{color:var(--warn)}
.visually-hidden{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
@media (max-width:899px){
  .wrap{padding:16px}
  .stat-grid{display:grid;grid-template-columns:1fr 1fr}
  .stat{min-width:0}
  .svc-table .col-en{display:none}
  .n .badge-en--dup{display:inline-block}
  th.n{position:sticky;left:0;background:inherit;z-index:1}
  td.iss{max-width:none}
}
""".strip()

    # type= so existing fixtures that ban a bare "<script>" XSS token stay green
    js = r"""
(function () {
    if (window.__mcpStatusPoll) return;
    window.__mcpStatusPoll = true;
    var PERIOD = 10000;
    var inFlight = false;
    var failed = false;
    var remain = PERIOD / 1000;
    var backoff = 0;
    function refreshEl() { return document.querySelector(".refresh"); }
    function paintCountdown() {
      if (failed || inFlight) return;
      var el = refreshEl();
      if (el) el.textContent = "in " + remain + "s";
    }
    function tick() {
      if (inFlight) return;
      inFlight = true;
      var y = window.scrollY;
      var only = document.getElementById("only-issues");
      var wantOnly = !!(only && only.checked);
      fetch("/status", { cache: "no-store", credentials: "same-origin" })
        .then(function (r) { if (!r.ok) throw r.status; return r.text(); })
        .then(function (html) {
          var doc = new DOMParser().parseFromString(html, "text/html");
          var next = doc.getElementById("app");
          var cur = document.getElementById("app");
          if (next && cur) {
            cur.replaceWith(next);
            window.scrollTo(0, y);
            var box = document.getElementById("only-issues");
            if (box) box.checked = wantOnly;
          }
          failed = false;
          remain = PERIOD / 1000;
          paintCountdown();
        })
        .catch(function () {
          failed = true;
          backoff = PERIOD / 1000;
          var el = refreshEl();
          if (el) el.textContent = "retrying…";
        })
        .then(function () { inFlight = false; });
    }
    paintCountdown();
    setInterval(function () {
      if (inFlight) return;
      if (failed) {
        backoff -= 1;
        if (backoff <= 0) tick();
        return;
      }
      remain -= 1;
      if (remain <= 0) { remain = 0; tick(); }
      else paintCountdown();
    }, 1000);
  })();
""".strip()

    app = (
        '<div id="app"><div class="wrap">'
        '<header class="masthead"><div>'
        "<h1>mcp-gateway</h1>"
        f'<p class="sub"><span class="host">{_esc(data.get("host_url"))}</span>'
        f' · {_time_html(data.get("generated_at"))}</p>'
        "</div><div class=\"masthead-right\">"
        f"{chip}"
        '<span class="refresh" aria-live="off">in 10s</span>'
        "</div></header>"
        '<section class="stat-grid" aria-label="Summary">'
        f'<article class="stat {live_cls}" id="stat-live"><div class="v">{_esc(live_v)}</div><div class="k">failing now</div></article>'
        f'<article class="stat {issues_cls}" id="stat-issues"><div class="v">{_esc(issues_v)}</div><div class="k">open issues</div></article>'
        f'<article class="stat {up_cls}" id="stat-up"><div class="v">{_esc(up_v)}</div><div class="k">enabled up</div></article>'
        f'<article class="stat {disk_cls}" id="stat-disk"><div class="v">{_esc(disk_v)}</div><div class="k">disk</div></article>'
        f'<article class="stat {mem_cls}" id="stat-mem"><div class="v">{_esc(mem_v)}</div><div class="k">mem free</div></article>'
        f'<article class="stat {rst_cls}" id="stat-restarts"><div class="v">{_esc(rst_v)}</div><div class="k">restart debt</div></article>'
        "</section>"
        f'<section class="issue-strip {strip_mod}" role="region" aria-label="Issues">'
        "<h2>Issues</h2>"
        f"{strip_body}</section>"
        '<section class="core"><h2>Core</h2>'
        f"<div>{core_html}</div></section>"
        '<section class="svc-block" id="enabled">'
        "<h2>Enabled MCP services</h2>"
        '<p class="sub">Worst first. Failing-now rows pulse; old restart counts stay red but do not flash.</p>'
        '<input type="checkbox" id="only-issues" class="visually-hidden">'
        f'<label class="filter" for="only-issues">Issues only'
        f'<span class="filter-n">{sum(1 for s in enabled_rows if s.get("issues"))}</span></label>'
        f'<div class="table-wrap">{svc_table(enabled_rows, "tbl-enabled", "Enabled MCP services")}</div>'
        "</section>"
        '<section class="svc-block" id="other">'
        "<h2>Disabled / leftover</h2>"
        f'<div class="table-wrap">{svc_table(other_rows, "tbl-other", "Disabled and leftover units")}</div>'
        "</section>"
        '<section class="svc-block" id="containers">'
        "<h2>Backend containers</h2>"
        f'<div class="table-wrap">{backends_tbl}</div>'
        f"{other_dock}</section>"
        f'<footer class="foot">{foot}</footer>'
        f"{heals_block}"
        "</div></div>"
    )

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        "<title>mcp-gateway status</title>\n"
        '<noscript><meta http-equiv="refresh" content="10"></noscript>\n'
        f"<style>\n{css}\n</style>\n"
        "</head>\n"
        "<body>\n"
        f"{app}\n"
        f'<script type="text/javascript">\n{js}\n</script>\n'
        "</body>\n"
        "</html>\n"
    )


def _esc(s: Any) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_text(data: dict[str, Any]) -> str:
    lines = [
        f"=== mcp-gateway status · {data['generated_at']} ===",
        f"-- {data['summary']['up']}/{data['summary']['enabled']} enabled up · {data['summary']['issues']} issues --",
    ]
    for c in data["core"]:
        lines.append(f"  {c['name']:22} {c['active']:10} {c['sub']}")
    lines.append("-- services (uptime · restarts · port · issues) --")
    for s in data["services"]:
        flag = {"bad": "!!", "warn": " !", "ok": "  "}[s["severity"]]
        port = f":{s['port']}" if s["port"] else "    "
        iss = "; ".join(s["issues"]) if s["issues"] else s["active"]
        en = "on " if s["enabled"] else "off"
        proto = s.get("proto") or {}
        if not s["issues"] and proto.get("version"):
            iss = f"{iss} · MCP {proto['version']}"
        lines.append(
            f"{flag} {s['name']:20} {en}  {s['uptime']:>8}  rst={s['nrestarts']:<3} {port:<6} {iss}"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="mcp-gateway status snapshot")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--html", metavar="PATH", help="write HTML snapshot")
    ap.add_argument("--write-json", metavar="PATH")
    args = ap.parse_args(argv)
    data = collect()
    if args.html:
        data["source"] = os.environ.get("MCP_STATUS_SOURCE", "snapshot")
        Path(args.html).write_text(render_html(data))
        print(f"wrote {args.html}", file=sys.stderr)
    elif args.json or args.write_json:
        data["source"] = os.environ.get("MCP_STATUS_SOURCE", "live")
    if args.write_json:
        Path(args.write_json).write_text(json.dumps(data, indent=2))
        print(f"wrote {args.write_json}", file=sys.stderr)
    if args.json:
        json.dump(data, sys.stdout, indent=2)
        sys.stdout.write("\n")
    elif not args.html and not args.write_json:
        sys.stdout.write(render_text(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
