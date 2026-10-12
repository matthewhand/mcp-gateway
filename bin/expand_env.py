#!/usr/bin/env python3
"""Shared `${VAR}` expansion for the servers.json manifest (stdlib only).

`bin/launch-server.sh` and the CLI launchers keep only `${VAR}` refs in
`servers.json`: values come from the gateway env files
(`~/.config/swarm/.env`, `mcp-gateway/.env`) and are expanded at launch time.
Two reference forms are understood:

    ${VAR}             value of VAR; when VAR is unset the literal "${VAR}"
                       is left in place (unchanged legacy behaviour)
    ${VAR:-default}    value of VAR when it is set and non-empty, else
                       `default` (may contain ':' and '/', but not '}')

The default form is how host-specific, non-secret knobs stay overridable
without editing the manifest — e.g. the gpt-terminal-plus upstream URL:

    "spec_url": "${GPT_TERMINAL_PLUS_URL:-http://localhost:3100}/openapi.json"

CLI (used by the launchers):

    expand_env.py --manifest PATH [--server NAME] --field F [--field G ...]
    expand_env.py --manifest PATH --server NAME --env
    expand_env.py --manifest PATH --raw          # expanded manifest text
    expand_env.py 'text with ${VAR:-default}'    # one arg per line
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Mapping

# Same shape the inline python snippets have always used.
REF = re.compile(r"\$\{([^}]+)\}")

# ${VAR:-default} → (name, default); plain ${VAR} → (ref, None).
DEFAULT_SEP = ":-"


def split_ref(ref: str) -> tuple[str, str | None]:
    """Split one reference body into (name, default|None)."""
    name, sep, default = ref.partition(DEFAULT_SEP)
    return name, (default if sep else None)


def expand(text: str, environ: Mapping[str, str] | None = None) -> str:
    """Expand `${VAR}` / `${VAR:-default}` references in *text*.

    Plain `${VAR}` keeps the literal when VAR is unset, so a missing secret
    stays visible instead of silently becoming an empty string.
    """
    env = os.environ if environ is None else environ

    def repl(match: re.Match[str]) -> str:
        ref = match.group(1)
        name, default = split_ref(ref)
        if default is not None:
            value = env.get(name)
            return value if value else default
        value = env.get(ref)
        return match.group(0) if value is None else value

    return REF.sub(repl, text)


def expand_json(path: str | Path, environ: Mapping[str, str] | None = None) -> dict:
    """Read a manifest and return it with every reference expanded."""
    env = os.environ if environ is None else environ
    return json.loads(expand(Path(path).read_text(encoding="utf-8"), env))


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("text", nargs="*", help="literal text to expand (no --manifest)")
    ap.add_argument("--manifest", help="path to servers.json / servers.example.json")
    ap.add_argument("--server", help="manifest key to read (e.g. gpt-terminal-plus)")
    ap.add_argument("--field", action="append", default=[], dest="fields",
                    help="field of that server to print (repeatable; space-separated)")
    ap.add_argument("--env", action="store_true",
                    help="print that server's env as KEY=VALUE lines")
    ap.add_argument("--raw", action="store_true", help="print the expanded manifest text")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    env = os.environ

    if not args.manifest:
        for item in args.text:
            sys.stdout.write(expand(item, env) + "\n")
        return 0

    manifest_path = Path(args.manifest)
    text = manifest_path.read_text(encoding="utf-8")
    if args.raw:
        sys.stdout.write(expand(text, env))
        return 0

    try:
        data = expand_json(manifest_path, env)
    except Exception as exc:
        sys.stderr.write(f"expand_env: cannot read {manifest_path}: {exc}\n")
        return 2
    if args.server:
        cfg = data.get(args.server) if isinstance(data, dict) else None
        if not isinstance(cfg, dict):
            sys.stderr.write(f"expand_env: no server '{args.server}' in {manifest_path}\n")
            return 2
    elif args.env or args.fields:
        sys.stderr.write("expand_env: --env / --field need --server\n")
        return 2

    if args.env:
        for key, value in (cfg.get("env") or {}).items():
            sys.stdout.write(f"{key}={value}\n")
    if args.fields:
        values = []
        for field in args.fields:
            if field not in cfg:
                sys.stderr.write(
                    f"expand_env: server '{args.server}' has no '{field}'\n")
                return 2
            values.append(str(cfg[field]))
        sys.stdout.write(" ".join(values) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
