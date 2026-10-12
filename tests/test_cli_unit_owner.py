#!/usr/bin/env python3
"""Look-only user-vs-system mcp-cli-server@ overlap checks. No secrets."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from cli_unit_owner import (  # noqa: E402
    check_conflicts,
    collect_names,
    conflict_message,
    first_token,
    format_report,
    in_play,
    names_from_manifest,
    names_from_systemctl_list,
    query_verb,
)

ROOT = Path(__file__).resolve().parents[1]


class StateRules(unittest.TestCase):
    def test_in_play_active_or_enabled(self):
        self.assertTrue(in_play("active", "disabled"))
        self.assertTrue(in_play("inactive", "enabled"))
        self.assertTrue(in_play("activating", "disabled"))
        self.assertTrue(in_play("inactive", "enabled-runtime"))
        self.assertFalse(in_play("inactive", "disabled"))
        self.assertFalse(in_play("failed", "disabled"))
        self.assertFalse(in_play("inactive", "not-found"))
        self.assertFalse(in_play("unknown", "unknown"))

    def test_first_token_normalizes_systemctl_noise(self):
        self.assertEqual(first_token("active\n"), "active")
        self.assertEqual(first_token("not-found"), "not-found")
        self.assertEqual(first_token(""), "unknown")
        self.assertEqual(first_token("Failed to connect to bus"), "failed")


class ConflictRules(unittest.TestCase):
    def test_intended_live_state_is_clean(self):
        # User linger unit owns acpx-cli; system copy stays disabled.
        self.assertIsNone(
            conflict_message("acpx-cli", "active", "enabled", "inactive", "disabled")
        )
        self.assertIsNone(
            conflict_message("acpx-cli", "active", "enabled", "inactive", "not-found")
        )

    def test_both_active_warns(self):
        msg = conflict_message("acpx-cli", "active", "enabled", "active", "disabled")
        self.assertIsNotNone(msg)
        self.assertIn("mcp-cli-server@acpx-cli", msg)
        self.assertIn("user active/enabled", msg)
        self.assertIn("system active/disabled", msg)

    def test_both_enabled_warns_even_if_inactive(self):
        msg = conflict_message("gh-cli", "inactive", "enabled", "inactive", "enabled")
        self.assertIsNotNone(msg)
        self.assertIn("mcp-cli-server@gh-cli", msg)

    def test_mixed_active_and_enabled_warns(self):
        msg = conflict_message("oci-cli", "active", "disabled", "inactive", "enabled")
        self.assertIsNotNone(msg)
        self.assertIn("oci-cli", msg)

    def test_system_only_does_not_warn(self):
        # Overlap is the hard-stop; a lone system instance is out of scope.
        self.assertIsNone(
            conflict_message("openscad", "inactive", "disabled", "active", "enabled")
        )


class Discovery(unittest.TestCase):
    def test_manifest_cli_keys_only(self):
        names = names_from_manifest(
            {
                "apisguru": {"port": 8808},
                "acpx-cli": {"port": 8826, "cli_port": 3870, "cli_config": "configs/acpx.json"},
                "openscad": {"port": 8816, "cli_config": "configs/openscad.json"},
                "skip": "not-a-block",
            }
        )
        self.assertEqual(names, ["acpx-cli", "openscad"])

    def test_example_manifest_includes_acpx_cli_class(self):
        data = json.loads((ROOT / "servers.example.json").read_text(encoding="utf-8"))
        names = names_from_manifest(data)
        self.assertIn("acpx-cli", names)
        self.assertEqual(set(names), {"acpx-cli", "gh-cli", "oci-cli", "openscad"})
        self.assertEqual(data["acpx-cli"]["port"], 8826)
        self.assertEqual(data["acpx-cli"]["cli_port"], 3870)
        env = data["acpx-cli"].get("env") or {}
        for value in env.values():
            self.assertNotRegex(str(value), r"(sk-|ghp_|Bearer [A-Za-z0-9_-]{12,})")

    def test_list_parser_strips_suffix_and_template(self):
        text = (
            "mcp-cli-server@acpx-cli.service loaded active running CLI\n"
            "mcp-cli-server@.service enabled\n"
            "mcp-cli-server@*.service\n"
        )
        self.assertEqual(names_from_systemctl_list(text), {"acpx-cli"})

    def test_collect_names_unions_manifest_and_leftovers(self):
        def run(cmd: list[str]) -> str:
            if "list-units" in cmd and "--user" in cmd:
                return "mcp-cli-server@leftover.service loaded active running leftover\n"
            return ""

        names = collect_names({"gh-cli": {"cli_port": 3868}}, run)
        self.assertEqual(names, ["gh-cli", "leftover"])


class Probe(unittest.TestCase):
    def test_query_verb_is_look_only(self):
        seen: list[list[str]] = []

        def run(cmd: list[str]) -> str:
            seen.append(cmd)
            return "inactive"

        self.assertEqual(query_verb(run, "user", "is-active", "mcp-cli-server@acpx-cli"), "inactive")
        self.assertEqual(query_verb(run, "system", "is-enabled", "mcp-cli-server@acpx-cli"), "inactive")
        for cmd in seen:
            self.assertEqual(cmd[0], "systemctl")
            self.assertTrue({"is-active", "is-enabled"} & set(cmd))
            self.assertEqual(cmd[-2], "--")
            self.assertNotIn("start", cmd)
            self.assertNotIn("stop", cmd)
            self.assertNotIn("restart", cmd)
            self.assertNotIn("enable", cmd)
            self.assertNotIn("disable", cmd)

    def test_check_conflicts_uses_mocked_systemctl_only(self):
        def run(cmd: list[str]) -> str:
            joined = " ".join(cmd)
            if "--user" in cmd and "is-active" in cmd:
                return "active"
            if "--user" in cmd and "is-enabled" in cmd:
                return "enabled"
            if "is-active" in joined:
                return "active"
            return "disabled"

        warnings = check_conflicts(["acpx-cli"], run)
        self.assertEqual(len(warnings), 1)
        self.assertIn("acpx-cli", warnings[0])

    def test_format_report_ok_and_warn(self):
        self.assertEqual(
            format_report([], []),
            ["ok: no mcp-cli-server@ instances to check"],
        )
        ok = format_report(["acpx-cli"], [])
        self.assertTrue(ok[0].startswith("ok:"))
        warn = format_report(["acpx-cli"], ["mcp-cli-server@acpx-cli: both in play"])
        self.assertTrue(warn[0].startswith("⚠"))
        self.assertIn("look-only", warn[-1])


class WiringAndDocs(unittest.TestCase):
    def test_validate_and_status_call_helper(self):
        validate = (ROOT / "bin" / "validate.sh").read_text(encoding="utf-8")
        status = (ROOT / "status.sh").read_text(encoding="utf-8")
        self.assertIn("cli_unit_owner.py", validate)
        self.assertIn("cli_unit_owner.py", status)
        self.assertNotRegex(validate, r"systemctl (start|stop|restart|enable|disable)")
        # status.sh already queries is-active; it must not mutate CLI units.
        self.assertNotRegex(status, r"systemctl(?: --user)? (start|stop|restart) mcp-cli-server")

    def test_runbook_hard_stop_and_acpx_example(self):
        text = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
        for token in (
            "hard-stop",
            "prefer",
            "user unit",
            "linger",
            "mcp-cli-server@",
            "disabled",
            "acpx-cli",
            ":3870",
            ":8826",
            "validate.sh",
            "is-active",
            "is-enabled",
        ):
            self.assertIn(token, text)

    def test_operator_hard_stop_and_acpx_example(self):
        text = (ROOT / "docs" / "OPERATOR.md").read_text(encoding="utf-8")
        for token in (
            "Hard-stops",
            "mcp-cli-server@",
            "linger",
            "disabled",
            "acpx-cli",
            ":3870",
            ":8826",
            "validate.sh",
        ):
            self.assertIn(token, text)

    def test_unit_file_comment_states_preference(self):
        text = (ROOT / "systemd" / "mcp-cli-server@.service").read_text(encoding="utf-8")
        self.assertIn("linger", text.lower())
        self.assertIn("disabled", text.lower())
        self.assertIn("3870", text)
        self.assertIn("8826", text)


class LookOnlyContract(unittest.TestCase):
    def test_helper_source_never_mutates_units(self):
        src = (ROOT / "bin" / "cli_unit_owner.py").read_text(encoding="utf-8")
        for verb in (" start ", " stop ", " restart ", " enable ", " disable ", " kill "):
            self.assertNotIn(verb, src)
        self.assertIn("is-active", src)
        self.assertIn("is-enabled", src)

    def test_main_does_not_call_real_systemctl_when_patched(self):
        from cli_unit_owner import main

        def run(_cmd: list[str]) -> str:
            return ""

        with patch("cli_unit_owner._default_run", side_effect=run):
            rc = main(["--manifest", str(ROOT / "servers.example.json")])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
