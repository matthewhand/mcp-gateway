#!/usr/bin/env python3
"""Unit tests for deadlock-safe probing. No live :8815 curl."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import safe_probe  # noqa: E402


MANIFEST = {
    "apisguru": {"port": 8808, "env": {}},
    "gpt-terminal-plus": {"port": 8815, "env": {}},
    "wordpress-dev": {"port": 8817, "env": {}},
    "box": {"port": 8810, "env": {"API_KEY": "${BOX_API_KEY}"}},
}


class PlanProbe(unittest.TestCase):
    def test_foreground_other_instance(self):
        plan = safe_probe.plan_probe(
            "apisguru",
            environ={},
            manifest=MANIFEST,
            tty=True,
        )
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["mode"], "foreground")
        self.assertFalse(plan["self_probe"])
        self.assertEqual(plan["url"], "http://127.0.0.1:8808/healthz")
        self.assertNotIn("8815", plan["url"])
        self.assertFalse(safe_probe.is_public_oauth_url(plan["url"]))

    def test_nested_terminal_plus_uses_alternate_not_8815(self):
        plan = safe_probe.plan_probe(
            "gpt-terminal-plus",
            environ={"MCP_SERVER_NAME": "gpt-terminal-plus", "MCP_PORT": "8815"},
            manifest=MANIFEST,
            tty=False,
        )
        self.assertTrue(plan["self_probe"])
        self.assertEqual(plan["mode"], "alternate+background")
        self.assertNotEqual(plan["alternate_port"], 8815)
        self.assertNotIn("8815", plan["alternate_url"] or "")
        self.assertEqual(plan["alternate_name"], "glama")
        self.assertEqual(plan["alternate_url"], "http://127.0.0.1:8765/healthz")
        self.assertIn("/tmp/mcp-safe-probe-gpt-terminal-plus.out", plan["background"]["out"])
        self.assertIn("&", plan["background"]["cmd"])
        for url in (plan["url"], plan["alternate_url"], plan["background"]["url"]):
            self.assertFalse(safe_probe.is_public_oauth_url(url))
            self.assertTrue(url.startswith("http://127.0.0.1:"))
        self.assertNotIn("https://", plan["background"]["cmd"])

    def test_non_tty_unknown_caller_still_guards_terminal_plus(self):
        plan = safe_probe.plan_probe(
            "gpt-terminal-plus",
            environ={},
            manifest=MANIFEST,
            tty=False,
        )
        self.assertTrue(plan["self_probe"])
        self.assertIsNotNone(plan["alternate_url"])
        self.assertNotIn(":8815", plan["alternate_url"])

    def test_operator_tty_may_foreground_terminal_plus(self):
        plan = safe_probe.plan_probe(
            "gpt-terminal-plus",
            environ={},
            manifest=MANIFEST,
            tty=True,
        )
        self.assertFalse(plan["self_probe"])
        self.assertEqual(plan["url"], "http://127.0.0.1:8815/healthz")

    def test_allow_foreground_override(self):
        plan = safe_probe.plan_probe(
            "gpt-terminal-plus",
            environ={"SAFE_PROBE_ALLOW_FOREGROUND": "1", "MCP_PORT": "8815"},
            manifest=MANIFEST,
            tty=False,
        )
        self.assertFalse(plan["self_probe"])
        self.assertEqual(plan["mode"], "foreground")

    def test_same_port_as_caller_is_self_probe(self):
        plan = safe_probe.plan_probe(
            "apisguru",
            environ={"MCP_GATEWAY_CALLER_PORT": "8808"},
            manifest=MANIFEST,
            tty=True,
        )
        self.assertTrue(plan["self_probe"])
        self.assertNotEqual(plan["alternate_port"], 8808)

    def test_unknown_name(self):
        plan = safe_probe.plan_probe("no-such", manifest=MANIFEST, tty=True, environ={})
        self.assertFalse(plan["ok"])

    def test_public_oauth_url_rejected(self):
        self.assertTrue(
            safe_probe.is_public_oauth_url(
                "https://mcp.example.com/oauth/ex/gpt-terminal-plus/mcp"
            )
        )
        with self.assertRaises(ValueError):
            safe_probe._http_get(
                "https://mcp.example.com/oauth/ex/gpt-terminal-plus/mcp"
            )

    def test_fallback_listen_only_when_healthz_missing(self):
        plan = safe_probe.plan_probe(
            "apisguru", environ={}, manifest=MANIFEST, tty=True
        )
        with patch.object(safe_probe, "_http_get", return_value=(404, "not found")):
            result = safe_probe.run_probe(
                plan, ss_out="LISTEN 0 511 127.0.0.1:8808 0.0.0.0:*\n"
            )
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["listen_only"])
        self.assertIn("listen-only", result["fallback"])

    def test_listen_down(self):
        plan = safe_probe.plan_probe(
            "apisguru", environ={}, manifest=MANIFEST, tty=True
        )
        with patch.object(safe_probe, "_http_get", return_value=(0, "refused")):
            result = safe_probe.run_probe(plan, ss_out="LISTEN 0 511 127.0.0.1:8765 0.0.0.0:*\n")
        self.assertEqual(result["status"], "down")

    def test_cli_plan(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "servers.json").write_text(json.dumps(MANIFEST))
            self.assertEqual(
                safe_probe.main(["--plan", "--root", str(root), "apisguru"]),
                0,
            )


if __name__ == "__main__":
    unittest.main()
