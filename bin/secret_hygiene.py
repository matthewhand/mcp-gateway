#!/usr/bin/env python3
"""Secret-hygiene rules for the servers.json manifest (P1 env-ref hardening).

The manifest must never carry literal secrets: API keys live in the gateway
env files (`~/.config/swarm/.env` and `mcp-gateway/.env`) and are referenced
from `servers.json` as `${VAR}` refs which `launch-server.sh` expands at
runtime. These rules make that contract enforceable:

  * an env value that contains `${VAR}` refs is fine (expanded at launch —
    `${VAR:-default}` refs count too: the default is a plain URL/path, never a key);
  * a secret-named env key (`API_KEY` / `*_API_KEY` / `TOKEN` / `SECRET` /
    `PASSWORD`) must be a `${VAR}` ref or empty — a literal is a violation;
  * no env value may embed a bare high-entropy token outside `${...}`
    (guards against `Api-Key sk-abc…` literals sneaking into EXTRA_HEADERS).

Deliberately NOT flagged: `API_AUTH_TYPE` / `API_AUTH_PARAM` / `API_AUTH_HEADER`
(provider auth-policy selectors like "Token", "api-key", "x-apikey"), whitelist
paths, URLs, and short values.
"""
from __future__ import annotations

import re

# Keys that must never hold a literal value. AUTH is excluded on purpose:
# API_AUTH_TYPE/PARAM/HEADER are selectors, not secrets.
SECRET_KEY = re.compile(r"(^|_)(API_KEY|TOKEN|SECRET|PASSWORD)$")

# A ${VAR} reference anywhere in the value means the launcher will expand it.
_REF = re.compile(r"\$\{[^}]+\}")

# Conservative bare-token sniff: a 20+ char run of token-ish chars not inside
# ${...}. URLs/paths/whitelists break it via / , { [ : spaces.
_BARE_TOKEN = re.compile(r"[A-Za-z0-9_\-]{20,}")

# Values that may legitimately contain long runs.
_ALLOWED_VALUE_PREFIXES = ("http://", "https://", "file://", "/")


def check_manifest_secrets(manifest: dict) -> list[str]:
    """Return hygiene violations for a parsed servers.json manifest ([] == clean)."""
    violations: list[str] = []
    for name, cfg in manifest.items():
        env = cfg.get("env") or {}
        if not isinstance(env, dict):
            continue
        for key, raw in env.items():
            value = str(raw)
            if SECRET_KEY.search(key):
                if not value:
                    continue
                if not (value.startswith("${") and value.endswith("}")):
                    ref = "${" + key.upper().replace("-", "_") + "}"
                    violations.append(
                        f"{name}.env.{key}: literal value — use {ref} ref; the key belongs in the gateway .env files"
                    )
                continue
            # Non-secret key: flag only if a bare long token sits outside ${...}.
            if not value or _REF.search(value):
                continue
            if _BARE_TOKEN.search(value) and not value.startswith(_ALLOWED_VALUE_PREFIXES):
                violations.append(f"{name}.env.{key}: possible embedded secret literal")
    return violations