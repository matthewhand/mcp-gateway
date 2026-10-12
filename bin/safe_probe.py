#!/usr/bin/env python3
"""Deadlock-safe health probe for mcp-gateway instances.

Probes loopback GET /healthz (native Streamable HTTP). Never foreground-curls
the caller's own port — nested probes of gpt-terminal-plus :8815 deadlock the
worker. Never uses the public OAuth MCP URL.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
HEALTH_PATH = "/healthz"
LOOPBACK = "127.0.0.1"
GLAMA_PORT = 8765
TERMINAL_PLUS = "gpt-terminal-plus"
# MCP proxy + upstream terminal API — never treat these as a "safe alternate".
# 8815 is the Terminal Plus MCP port; 3100 is the default
# GPT_TERMINAL_PLUS_PORT (the upstream HTTP API the gateway proxies to).
DANGER_PORTS = {8815, 3100}
PUBLIC_OAUTH_HINTS = (
      "/oauth/ex/",
      "/oauth/mcp",
      # Host-specific; from env so the repo stays portable. The bare
      # host/path forms are kept as hints regardless of deployment name.
      (os.getenv("MCP_PUBLIC_HOSTNAME") or "mcp.example.com"),
  )
FALLBACK = (
    "listen-only via `ss -tlnH` on 127.0.0.1:<port> "
    "(GET /healthz 404/missing — sister mcp-openapi-proxy #66; "
    "never POST /mcp and never the public OAuth URL)"
)


def load_manifest(root: Path | None = None) -> dict[str, Any]:
    path = Path(root or ROOT) / "servers.json"
    if not path.is_file():
        path = Path(root or ROOT) / "servers.example.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def instance_port(name: str, manifest: Mapping[str, Any] | None = None) -> int | None:
    if name in {"glama", "mcp-glama"}:
        return GLAMA_PORT
    manifest = manifest if manifest is not None else load_manifest()
    cfg = manifest.get(name) if isinstance(manifest, dict) else None
    if not isinstance(cfg, dict):
        return None
    port = cfg.get("port")
    try:
        return int(port)
    except (TypeError, ValueError):
        return None


def health_url(port: int, path: str = HEALTH_PATH) -> str:
    return f"http://{LOOPBACK}:{int(port)}{path}"


def is_public_oauth_url(url: str) -> bool:
    raw = (url or "").lower()
    return any(hint in raw for hint in PUBLIC_OAUTH_HINTS)


def caller_port(
    environ: Mapping[str, str] | None = None,
    manifest: Mapping[str, Any] | None = None,
) -> int | None:
    env = environ if environ is not None else os.environ
    for key in ("MCP_GATEWAY_CALLER_PORT", "MCP_PORT"):
        raw = (env.get(key) or "").strip()
        if raw.isdigit():
            return int(raw)
    name = (env.get("MCP_SERVER_NAME") or "").strip()
    if name:
        return instance_port(name, manifest)
    return None


def caller_is_nested_terminal(
    environ: Mapping[str, str] | None = None,
    *,
    tty: bool | None = None,
) -> bool:
    env = environ if environ is not None else os.environ
    if (env.get("SAFE_PROBE_ALLOW_FOREGROUND") or "").strip() == "1":
        return False
    if (env.get("SAFE_PROBE_ASSUME_NESTED") or "").strip() == "1":
        return True
    name = (env.get("MCP_SERVER_NAME") or "").strip()
    if name == TERMINAL_PLUS:
        return True
    port = caller_port(env)
    if port in DANGER_PORTS:
        return True
    if tty is None:
        tty = sys.stdin.isatty()
    # Tool-call shells from Terminal Plus are typically non-tty.
    return (not tty) and name == TERMINAL_PLUS


def would_self_probe(
    name: str,
    port: int,
    *,
    environ: Mapping[str, str] | None = None,
    manifest: Mapping[str, Any] | None = None,
    tty: bool | None = None,
) -> bool:
    env = environ if environ is not None else os.environ
    if (env.get("SAFE_PROBE_ALLOW_FOREGROUND") or "").strip() == "1":
        return False
    cport = caller_port(env, manifest)
    if cport is not None and int(cport) == int(port):
        return True
    caller_name = (env.get("MCP_SERVER_NAME") or "").strip()
    if caller_name and caller_name == name:
        return True
    if name == TERMINAL_PLUS and caller_is_nested_terminal(env, tty=tty):
        return True
    # Unknown caller + probing Terminal Plus from a non-tty: stay safe.
    if name == TERMINAL_PLUS and cport is None:
        if tty is None:
            tty = sys.stdin.isatty()
        if not tty:
            return True
    return False


def pick_alternate(
    target_name: str,
    caller_port_n: int | None,
    manifest: Mapping[str, Any] | None = None,
) -> tuple[str, int] | None:
    manifest = manifest if manifest is not None else load_manifest()
    prefer = ("glama", "apisguru")  # wordpress-dev removed 2026-09-06 (disabled in manifest)
    rows: list[tuple[str, int]] = [("glama", GLAMA_PORT)]
    if isinstance(manifest, dict):
        for name, cfg in manifest.items():
            if not isinstance(cfg, dict):
                continue
            try:
                rows.append((name, int(cfg["port"])))
            except (KeyError, TypeError, ValueError):
                continue
    seen: set[str] = set()
    ordered: list[tuple[str, int]] = []
    by_name = {n: p for n, p in rows}
    for name in prefer:
        if name in by_name:
            ordered.append((name, by_name[name]))
            seen.add(name)
    for name, port in rows:
        if name not in seen:
            ordered.append((name, port))
            seen.add(name)
    for name, port in ordered:
        if name == target_name:
            continue
        if name == TERMINAL_PLUS or port in DANGER_PORTS:
            continue
        if caller_port_n is not None and port == int(caller_port_n):
            continue
        return name, port
    return None


def background_recipe(name: str, port: int, out_path: str | None = None) -> dict[str, str]:
    out = out_path or f"/tmp/mcp-safe-probe-{name}.out"
    url = health_url(port)
    cmd = (
        f"curl -sS --max-time 2 -o {out} -w '%{{http_code}}\\n' {url} "
        f"> /tmp/mcp-safe-probe-{name}.code 2>&1 &"
    )
    return {"cmd": cmd, "out": out, "url": url}


def plan_probe(
    name: str,
    *,
    environ: Mapping[str, str] | None = None,
    manifest: Mapping[str, Any] | None = None,
    tty: bool | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    manifest = manifest if manifest is not None else load_manifest(root)
    env = environ if environ is not None else os.environ
    port = instance_port(name, manifest)
    if port is None:
        return {
            "ok": False,
            "error": f"unknown instance {name!r}",
            "name": name,
        }
    url = health_url(port)
    if is_public_oauth_url(url):
        return {"ok": False, "error": "refusing public OAuth URL", "name": name}
    cport = caller_port(env, manifest)
    self = would_self_probe(name, port, environ=env, manifest=manifest, tty=tty)
    plan: dict[str, Any] = {
        "ok": True,
        "name": name,
        "port": port,
        "url": url,
        "fallback": FALLBACK,
        "self_probe": self,
        "caller_port": cport,
        "mode": "foreground",
        "alternate_name": None,
        "alternate_port": None,
        "alternate_url": None,
        "background": None,
        "forbid": [
            "foreground curl of the caller's own port",
            "foreground curl of 127.0.0.1:8815 from inside Terminal Plus",
            "public OAuth MCP URL (https://…/oauth/ex/<name>/mcp)",
            "POST /mcp (takes the Streamable HTTP worker lock)",
        ],
    }
    if not self:
        return plan
    alt = pick_alternate(name, cport if cport is not None else port, manifest)
    bg = background_recipe(name, port)
    plan["mode"] = "alternate+background"
    plan["background"] = bg
    plan["reason"] = (
        f"refusing foreground probe of :{port} ({name}) — that is the caller's "
        "own port (or gpt-terminal-plus nested in a tool call). "
        "Use another instance or the background curl-to-/tmp recipe."
    )
    if alt:
        plan["alternate_name"], plan["alternate_port"] = alt
        plan["alternate_url"] = health_url(alt[1])
    return plan


def _http_get(url: str, timeout: float = 2.0) -> tuple[int, str]:
    if is_public_oauth_url(url):
        raise ValueError("refusing public OAuth URL")
    parsed = urlparse(url)
    if parsed.hostname not in {LOOPBACK, "localhost", "::1"}:
        raise ValueError("refusing non-loopback probe")
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(4096).decode("utf-8", errors="replace")
            return int(resp.status), body
    except urllib.error.HTTPError as e:
        body = e.read(4096).decode("utf-8", errors="replace") if e.fp else ""
        return int(e.code), body


def listen_only(port: int, ss_out: str | None = None) -> bool:
    if ss_out is None:
        try:
            p = subprocess.run(["ss", "-tlnH"], capture_output=True, text=True, timeout=4)
            ss_out = p.stdout
        except Exception:
            return False
    return bool(re.search(rf":{int(port)}\s", (ss_out or "").replace("\t", " ") + " "))


def run_probe(plan: dict[str, Any], *, timeout: float = 2.0, ss_out: str | None = None) -> dict[str, Any]:
    result = dict(plan)
    target_url = plan.get("alternate_url") if plan.get("self_probe") else plan.get("url")
    target_name = plan.get("alternate_name") if plan.get("self_probe") else plan.get("name")
    target_port = plan.get("alternate_port") if plan.get("self_probe") else plan.get("port")
    result["probed_name"] = target_name
    result["probed_url"] = target_url
    result["probed_port"] = target_port
    if not target_url or target_port is None:
        result["status"] = "error"
        result["detail"] = "no safe target"
        return result
    try:
        code, body = _http_get(str(target_url), timeout=timeout)
    except Exception as e:
        code, body = 0, str(e)
    result["http_code"] = code
    # Do not echo large/unexpected bodies; healthz is a small JSON.
    result["body"] = body[:400] if body else ""
    if code == 200:
        result["status"] = "ok"
        result["detail"] = f"GET {target_url} -> 200"
        return result
    if code in {404, 405} or code == 0:
        listening = listen_only(int(target_port), ss_out=ss_out)
        result["listen_only"] = listening
        result["status"] = "ok" if listening else "down"
        result["detail"] = (
            f"GET {target_url} -> {code or 'error'}; fallback {FALLBACK}; "
            f"listening={'yes' if listening else 'no'}"
        )
        return result
    result["status"] = "unhealthy"
    result["detail"] = f"GET {target_url} -> {code}"
    return result


def _start_background(plan: dict[str, Any]) -> None:
    bg = plan.get("background") or {}
    cmd = bg.get("cmd")
    if not cmd:
        return
    # Fire-and-forget; stdout/err already redirected in the recipe.
    subprocess.Popen(["bash", "-lc", cmd], start_new_session=True)


def render_text(result: dict[str, Any]) -> str:
    lines = []
    if not result.get("ok") and result.get("error"):
        return f"safe-probe: {result['error']}\n"
    name = result.get("name")
    port = result.get("port")
    lines.append(f"safe-probe {name} :{port}")
    if result.get("self_probe"):
        lines.append(f"  SELF-PROBE GUARD: {result.get('reason')}")
        if result.get("alternate_name"):
            lines.append(
                f"  probing alternate {result['alternate_name']} "
                f"{result.get('alternate_url')}"
            )
        bg = result.get("background") or {}
        if bg:
            lines.append("  background self-check (do not foreground this from Terminal Plus):")
            lines.append(f"    {bg.get('cmd')}")
            lines.append(f"    # then: cat {bg.get('out')}")
    else:
        lines.append(f"  GET {result.get('url')}")
    if result.get("detail"):
        lines.append(f"  {result['detail']}")
    if result.get("status"):
        lines.append(f"  status={result['status']}")
    lines.append(f"  fallback: {result.get('fallback')}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Deadlock-safe /healthz probe (loopback only; never public OAuth)"
    )
    ap.add_argument("name", nargs="?", help="servers.json instance name (or glama)")
    ap.add_argument("--plan", action="store_true", help="print plan JSON; do not curl")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--root", type=Path, default=None)
    args = ap.parse_args(argv)
    if not args.name:
        ap.print_help()
        sys.stderr.write("\nusage: safe-probe <name>\n")
        return 2
    plan = plan_probe(args.name, root=args.root)
    if args.plan:
        json.dump(plan, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0 if plan.get("ok") else 2
    if not plan.get("ok"):
        sys.stderr.write(render_text(plan))
        return 2
    result = run_probe(plan)
    if result.get("self_probe"):
        _start_background(plan)
    if args.json:
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_text(result))
    if result.get("status") == "ok":
        return 0
    if result.get("status") == "down":
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
