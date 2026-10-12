#!/usr/bin/env python3
"""Box API key presence / expiry gate. Never prints secret values."""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))

from expand_env import expand  # noqa: E402

REASON = "short-lived Box token; refresh BOX_API_KEY"
PLACEHOLDERS = {"", "${BOX_API_KEY}", "<REDACTED>", "REDACTED"}

ROOT = Path(__file__).resolve().parent.parent


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        out[key.strip()] = value
    return out


def _is_placeholder(value: str | None) -> bool:
    v = (value or "").strip()
    if v in PLACEHOLDERS:
        return True
    return bool(re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", v))


def jwt_exp_unix(token: str) -> int | None:
    """Return JWT ``exp`` if the value looks like a 3-part JWT; never logs the token."""
    parts = (token or "").split(".")
    if len(parts) != 3 or not all(parts):
        return None
    payload = parts[1]
    pad = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + pad)
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    exp = data.get("exp") if isinstance(data, dict) else None
    try:
        return int(exp)
    except (TypeError, ValueError):
        return None


def _read_key_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _manifest_api_key(manifest: Mapping[str, Any] | None, environ: Mapping[str, str]) -> str:
    if not manifest:
        return ""
    box = manifest.get("box") if isinstance(manifest, dict) else None
    if not isinstance(box, dict):
        return ""
    env = box.get("env") if isinstance(box.get("env"), dict) else {}
    raw = str(env.get("API_KEY") or "")
    # ${VAR} / ${VAR:-default} refs are expanded here (see bin/expand_env.py).
    return expand(raw, environ)


def default_env_files(root: Path | None = None) -> list[Path]:
    root = Path(root or ROOT)
    home = Path(os.environ.get("HOME") or Path.home())
    return [
        root / ".env",
        home / ".config" / "swarm" / ".env",
        home / ".config" / "mcp-gateway" / "box.env",
    ]


def default_key_files() -> list[Path]:
    home = Path(os.environ.get("HOME") or Path.home())
    extra = os.environ.get("MCP_BOX_KEY_FILE") or ""
    paths = [
        home / ".config" / "mcp-gateway" / "box-api-key",
    ]
    if extra:
        paths.insert(0, Path(extra))
    return paths


def box_key_status(
    *,
    root: Path | None = None,
    manifest: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    now: int | None = None,
    env_files: Iterable[Path] | None = None,
    key_files: Iterable[Path] | None = None,
) -> dict[str, Any]:
    """Presence + optional JWT expiry. Values never appear in the result."""
    import time

    root = Path(root or ROOT)
    environ = dict(os.environ if environ is None else environ)
    if manifest is None:
        path = root / "servers.json"
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}

    merged: dict[str, str] = {}
    for path in list(env_files if env_files is not None else default_env_files(root)):
        merged.update(_parse_env_file(Path(path)))
    merged.update({k: v for k, v in environ.items() if v is not None})

    value = (merged.get("BOX_API_KEY") or "").strip()
    if _is_placeholder(value):
        for path in list(key_files if key_files is not None else default_key_files()):
            value = _read_key_file(Path(path))
            if value and not _is_placeholder(value):
                break
    if _is_placeholder(value):
        value = _manifest_api_key(manifest, merged)

    present = not _is_placeholder(value)
    expired = False
    if present:
        exp = jwt_exp_unix(value)
        if exp is not None:
            stamp = int(time.time() if now is None else now)
            expired = exp <= stamp
    unhealthy = (not present) or expired
    return {
        "present": present,
        "expired": expired,
        "unhealthy": unhealthy,
        "reason": REASON if unhealthy else None,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Box key gate (names/status only; no secret dump)")
    ap.add_argument("--check", action="store_true", help="exit 0 if usable, 1 if empty/expired")
    ap.add_argument("--root", type=Path, default=None)
    ap.add_argument("--manifest", type=Path, default=None)
    args = ap.parse_args(argv)
    root = args.root or ROOT
    manifest = None
    if args.manifest:
        try:
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}
    status = box_key_status(root=root, manifest=manifest)
    if args.check:
        return 1 if status["unhealthy"] else 0
    json.dump(status, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
