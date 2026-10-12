#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from status_collect import (  # noqa: E402
    classify,
    collect,
    format_uptime,
    main,
    parse_listening_ports,
    parse_show_blocks,
    parse_systemd_timestamp,
    pulse_for,
    render_html,
    render_text,
    unit_for,
)


class FormatAndParse(unittest.TestCase):
    def test_format_uptime(self):
        self.assertEqual(format_uptime(None), "—")
        self.assertEqual(format_uptime(9), "9s")
        self.assertEqual(format_uptime(60), "1m")
        self.assertEqual(format_uptime(75), "1m 15s")
        self.assertEqual(format_uptime(3600), "1h 0m")
        self.assertEqual(format_uptime(90000), "1d 1h")

    def test_parse_systemd_timestamp(self):
        now = dt.datetime(2026, 8, 18, 12, 0, 0, tzinfo=dt.timezone.utc)
        self.assertIsNone(parse_systemd_timestamp("n/a", now))
        self.assertEqual(parse_systemd_timestamp("Tue 2026-08-18 11:50:00 UTC", now), 600)
        self.assertIsNone(parse_systemd_timestamp("", now))

    def test_parse_show_blocks_indexes_instance(self):
        text = """
Id=mcp-server@github.service
ActiveState=active
NRestarts=2

Id=mcp-cli-server@openscad.service
ActiveState=active
NRestarts=6
"""
        blocks = parse_show_blocks(text)
        self.assertEqual(blocks["mcp-server@github"]["NRestarts"], "2")
        self.assertEqual(blocks["github"]["ActiveState"], "active")
        self.assertEqual(blocks["openscad"]["NRestarts"], "6")

    def test_parse_listening_ports(self):
        ss = "LISTEN 0 511 127.0.0.1:8801 0.0.0.0:*\nLISTEN 0 511 127.0.0.1:8765 0.0.0.0:*\n"
        self.assertEqual(parse_listening_ports(ss), {8801, 8765})

    def test_unit_for(self):
        self.assertEqual(unit_for("glama", {}), "mcp-glama")
        self.assertEqual(unit_for("github", {"port": 8812}), "mcp-server@github")
        self.assertEqual(unit_for("openscad", {"cli_port": 3860}), "mcp-cli-server@openscad")


class Classify(unittest.TestCase):
    def test_flapping_is_bad(self):
        sev, issues = classify({
            "expected": True, "enabled": True, "manual": False, "leftover": False,
            "listening": True, "port": 8808, "active": "active", "sub": "running",
            "nrestarts": 10, "uptime_sec": 72000, "load": "loaded",
        })
        self.assertEqual(sev, "bad")
        self.assertTrue(any("flapping" in i for i in issues))

    def test_restarting_now(self):
        sev, issues = classify({
            "expected": True, "enabled": True, "manual": False, "leftover": False,
            "listening": False, "port": 8801, "active": "activating", "sub": "auto-restart",
            "nrestarts": 4, "uptime_sec": 3, "load": "loaded",
        })
        self.assertEqual(sev, "bad")
        self.assertTrue(any("restarting now" in i for i in issues))

    def test_short_uptime_after_restart(self):
        sev, issues = classify({
            "expected": True, "enabled": True, "manual": False, "leftover": False,
            "listening": True, "port": 8801, "active": "active", "sub": "running",
            "nrestarts": 2, "uptime_sec": 90, "load": "loaded",
        })
        self.assertEqual(sev, "bad")
        self.assertTrue(any("just started" in i for i in issues))

    def test_down_enabled(self):
        sev, issues = classify({
            "expected": True, "enabled": True, "manual": False, "leftover": False,
            "listening": False, "port": 8812, "active": "failed", "sub": "failed",
            "nrestarts": 0, "uptime_sec": None, "load": "loaded",
        })
        self.assertEqual(sev, "bad")
        self.assertTrue(any("down" in i for i in issues))

    def test_box_manual_off_is_ok(self):
        sev, issues = classify({
            "expected": True, "enabled": False, "manual": True, "leftover": False,
            "listening": False, "port": 8810, "active": "inactive", "sub": "dead",
            "nrestarts": 0, "uptime_sec": None, "load": "loaded",
        })
        self.assertEqual(sev, "ok")
        self.assertEqual(issues, [])

    def test_leftover_masked(self):
        sev, issues = classify({
            "expected": False, "enabled": False, "manual": False, "leftover": True,
            "listening": False, "port": None, "active": "failed", "sub": "failed",
            "nrestarts": 0, "uptime_sec": None, "load": "masked",
        })
        self.assertEqual(sev, "warn")
        self.assertTrue(any("leftover" in i for i in issues))

    def test_old_single_restart_is_table_only(self):
        sev, issues = classify({
            "expected": True, "enabled": True, "manual": False, "leftover": False,
            "listening": True, "port": 8803, "active": "active", "sub": "running",
            "nrestarts": 1, "uptime_sec": 200000, "load": "loaded",
        })
        self.assertEqual(sev, "ok")
        self.assertEqual(issues, [])

    def test_not_enabled(self):
        sev, issues = classify({
            "expected": True, "enabled": False, "manual": False, "leftover": False,
            "listening": False, "port": 8801, "active": "inactive", "sub": "dead",
            "nrestarts": 0, "uptime_sec": None, "load": "loaded",
        })
        self.assertEqual(sev, "bad")
        self.assertTrue(any("not enabled" in i for i in issues))

    def test_stale_flap_pulse_false(self):
        row = {
            "expected": True, "enabled": True, "manual": False, "leftover": False,
            "listening": True, "port": 8808, "active": "active", "sub": "running",
            "nrestarts": 10, "uptime_sec": 72000, "load": "loaded",
        }
        sev, issues = classify(row)
        self.assertEqual(sev, "bad")
        self.assertFalse(pulse_for(row, sev, issues))

    def test_live_flap_pulse_true(self):
        row = {
            "expected": True, "enabled": True, "manual": False, "leftover": False,
            "listening": True, "port": 8808, "active": "active", "sub": "running",
            "nrestarts": 10, "uptime_sec": 30, "load": "loaded",
        }
        sev, issues = classify(row)
        self.assertEqual(sev, "bad")
        self.assertTrue(pulse_for(row, sev, issues))

    def test_restarting_now_pulse_true(self):
        row = {
            "expected": True, "enabled": True, "manual": False, "leftover": False,
            "listening": False, "port": 8801, "active": "activating", "sub": "auto-restart",
            "nrestarts": 4, "uptime_sec": 3, "load": "loaded",
        }
        sev, issues = classify(row)
        self.assertEqual(sev, "bad")
        self.assertTrue(pulse_for(row, sev, issues))

    def test_just_started_n0_pulse_false(self):
        row = {
            "expected": True, "enabled": True, "manual": False, "leftover": False,
            "listening": True, "port": 8801, "active": "active", "sub": "running",
            "nrestarts": 0, "uptime_sec": 90, "load": "loaded",
        }
        sev, issues = classify(row)
        self.assertEqual(sev, "warn")
        self.assertTrue(any("just started" in i for i in issues))
        self.assertFalse(pulse_for(row, sev, issues))

    def test_short_uptime_warn_pulse_false(self):
        row = {
            "expected": True, "enabled": True, "manual": False, "leftover": False,
            "listening": True, "port": 8801, "active": "active", "sub": "running",
            "nrestarts": 2, "uptime_sec": 300, "load": "loaded",
        }
        sev, issues = classify(row)
        self.assertEqual(sev, "warn")
        self.assertTrue(any("short uptime" in i for i in issues))
        self.assertFalse(pulse_for(row, sev, issues))

    def test_handshake_failure_is_bad_and_pulses(self):
        row = {
            "expected": True, "enabled": True, "manual": False, "leftover": False,
            "listening": True, "port": 8812, "active": "active", "sub": "running",
            "nrestarts": 0, "uptime_sec": 72000, "load": "loaded",
            "proto": {"ok": False, "version": None, "protocol": None, "error": "HTTP 500"},
        }
        sev, issues = classify(row)
        self.assertEqual(sev, "bad")
        self.assertTrue(any(i.startswith("MCP handshake failed") for i in issues))
        self.assertTrue(pulse_for(row, sev, issues))

    def test_handshake_ok_adds_no_issue(self):
        row = {
            "expected": True, "enabled": True, "manual": False, "leftover": False,
            "listening": True, "port": 8812, "active": "active", "sub": "running",
            "nrestarts": 0, "uptime_sec": 72000, "load": "loaded",
            "proto": {"ok": True, "version": "0.4.0", "protocol": "2025-11-25", "error": None},
        }
        sev, issues = classify(row)
        self.assertEqual(sev, "ok")
        self.assertFalse(any("handshake" in i for i in issues))
        self.assertFalse(pulse_for(row, sev, issues))

    def test_handshake_ignored_when_not_listening(self):
        # A dead/disabled bridge carries no proto result — never invent an issue.
        row = {
            "expected": True, "enabled": True, "manual": False, "leftover": False,
            "listening": False, "port": 8812, "active": "failed", "sub": "failed",
            "nrestarts": 0, "uptime_sec": None, "load": "loaded",
            "proto": {"ok": False, "version": None, "protocol": None, "error": "HTTP 500"},
        }
        sev, issues = classify(row)
        self.assertTrue(any("down" in i for i in issues))
        self.assertFalse(any("handshake" in i for i in issues))


class CollectLiveShape(unittest.TestCase):
    def test_collect_sorts_issues_and_hides_secrets(self):
        now = dt.datetime(2026, 8, 18, 12, 0, 0, tzinfo=dt.timezone.utc)
        show = """
Id=mcp-glama.service
ActiveState=active
SubState=running
UnitFileState=enabled
NRestarts=0
ActiveEnterTimestamp=Sun 2026-08-16 01:00:00 UTC
LoadState=loaded

Id=mcp-oauth.service
ActiveState=active
SubState=running
UnitFileState=enabled
NRestarts=0
ActiveEnterTimestamp=Sun 2026-08-16 01:00:00 UTC
LoadState=loaded

Id=mcp-watchdog.timer
ActiveState=active
SubState=waiting
UnitFileState=enabled
LoadState=loaded

Id=mcp-server@github.service
ActiveState=active
SubState=running
UnitFileState=enabled
NRestarts=0
ActiveEnterTimestamp=Sun 2026-08-16 01:32:05 UTC
LoadState=loaded

Id=mcp-server@apisguru.service
ActiveState=activating
SubState=auto-restart
UnitFileState=enabled
NRestarts=10
ActiveEnterTimestamp=Tue 2026-08-18 11:59:50 UTC
LoadState=loaded

Id=mcp-server@box.service
ActiveState=inactive
SubState=dead
UnitFileState=disabled
NRestarts=0
LoadState=loaded

Id=mcp-server@wordpress-dev.service
ActiveState=inactive
SubState=dead
UnitFileState=disabled
NRestarts=0
LoadState=loaded

Id=mcp-cli-server@openscad.service
ActiveState=active
SubState=running
UnitFileState=enabled
NRestarts=6
ActiveEnterTimestamp=Tue 2026-08-18 09:17:36 UTC
LoadState=loaded

Id=mcp-cli-server@vibe-cli.service
ActiveState=failed
SubState=failed
UnitFileState=masked
NRestarts=0
LoadState=masked

Id=mcp-status.service
ActiveState=active
SubState=running
UnitFileState=enabled
NRestarts=0
ActiveEnterTimestamp=Sun 2026-08-16 01:00:00 UTC
LoadState=loaded
"""
        shown_units: list[str] = []

        def show_runner(units):
            shown_units.extend(list(units))
            if not any("vibe-cli" in str(u) for u in units):
                return show.replace("Id=mcp-cli-server@vibe-cli.service", "Id=mcp-cli-server@omitted.service")
            return show

        runners = {
            "ss": lambda: "LISTEN 0 511 127.0.0.1:8812 0.0.0.0:*\nLISTEN 0 511 127.0.0.1:8765 0.0.0.0:*\nLISTEN 0 511 127.0.0.1:8816 0.0.0.0:*\n",
            "show": show_runner,
            "show_system": lambda units: "Id=nginx.service\nActiveState=active\nSubState=running\nUnitFileState=enabled\n",
            "list_units": lambda: "mcp-cli-server@vibe-cli.service masked failed failed leftover\n",
            "docker": lambda: "zep-ce-zep-1|Up 2 hours (healthy)|running\n",
            "free": lambda: "Mem: 16000 4000 2000 0 1000 8000\nSwap: 2559 100 2459\n",
            "df": lambda: "Filesystem Size Used Avail Use% Mounted\n/dev/sda 30G 20G 8G 72% /\n",
            "watchdog": lambda: "2026-08-18T11:59:56Z BRIDGE apisguru (:8808) down while unit active -> restart mcp-server@apisguru\n",
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "servers.json").write_text(json.dumps({
                "github": {
                    "port": 8812,
                    "spec_url": "https://example.invalid/openapi.json",
                    "env": {
                        "API_KEY": "SUPER-SECRET-DO-NOT-LEAK",
                        "EXTRA_HEADERS": "X-Internal: FAKE-EXTRA-HEADERS-DO-NOT-LEAK",
                    },
                },
                "apisguru": {"port": 8808, "env": {}},
                "box": {"port": 8810, "env": {"API_KEY": "BOXSECRET"}},
                "openscad": {"port": 8816, "cli_port": 3860, "env": {}},
                "wordpress-dev": {"port": 8817, "env": {}, "enabled": False},
            }))
            data = collect(root, now=now, runners=runners, environ={})
        names = [s["name"] for s in data["services"]]
        self.assertEqual(names[0], "apisguru")
        by = {s["name"]: s for s in data["services"]}
        self.assertEqual(by["apisguru"]["severity"], "bad")
        self.assertTrue(by["apisguru"]["pulse"])
        self.assertEqual(by["openscad"]["nrestarts"], 6)
        self.assertFalse(by["openscad"]["pulse"])
        self.assertTrue(by["vibe-cli"]["leftover"])
        self.assertEqual(by["vibe-cli"]["active"], "failed")
        self.assertTrue(any("vibe-cli" in str(u) for u in shown_units))
        self.assertTrue(by["box"]["manual"])
        self.assertEqual(by["box"]["severity"], "ok")
        # enabled:false manifest entries are intentionally offline: reported
        # as not-expected (Disabled table), never flagged as a port-down bad.
        self.assertFalse(by["wordpress-dev"]["expected"])
        self.assertEqual(by["wordpress-dev"]["severity"], "ok")
        self.assertFalse(by["wordpress-dev"]["pulse"])
        self.assertFalse(by["wordpress-dev"]["listening"])
        self.assertEqual(data["schema_version"], 1)
        self.assertNotIn("source", data)
        self.assertEqual(data["summary"]["live"], data["summary"]["restarting"])
        self.assertIn("mcp-status", [c["name"] for c in data["core"]])
        self.assertIsInstance(data["host"]["issues"], list)
        self.assertEqual(data["host"]["issues"], [])
        blob = json.dumps(data) + render_html(data)
        self.assertNotIn("SUPER-SECRET-DO-NOT-LEAK", blob)
        self.assertNotIn("BOXSECRET", blob)
        self.assertNotIn("FAKE-EXTRA-HEADERS-DO-NOT-LEAK", blob)
        self.assertNotIn("spec_url", blob)
        self.assertGreaterEqual(data["summary"]["issues"], 2)
        html = render_html(data)
        self.assertIn("apisguru", html)
        self.assertIn("restarting now", html)

    def test_html_escapes(self):
        html = render_html({
            "generated_at": "now",
            "host_url": "https://mcp.example.com",
            "summary": {"enabled": 0, "manifest": 0, "up": 0, "issues": 1, "restarting": 1, "restarts_total": 0},
            "core": [{"name": "nginx", "active": "active", "sub": "running", "severity": "ok", "issues": []}],
            "services": [{
                "name": "<xss>", "kind": "openapi", "badge": "keyed", "enabled": True,
                "unit_file_state": "enabled", "active": "failed", "sub": "failed",
                "uptime": "—", "uptime_sec": None, "nrestarts": 0, "port": 1,
                "listening": False, "severity": "bad", "issues": ["down <script>"],
                "expected": True, "manual": False, "leftover": False,
            }],
            "containers": [],
            "watchdog": {"last": "-", "heals": 0, "recent_heals": []},
            "host": {"mem_avail_mb": 1000, "swap_used_mb": 0, "swap_tot_mb": 1, "disk_used": "1%", "disk_free": "9G"},
        })
        self.assertNotIn("<xss>", html)
        self.assertIn("&lt;xss&gt;", html)
        self.assertNotIn("<script>", html)

    def test_leftover_active_comes_from_show(self):
        now = dt.datetime(2026, 8, 18, 12, 0, 0, tzinfo=dt.timezone.utc)

        def show(units):
            blocks = []
            joined = " ".join(str(u) for u in units)
            if "mcp-oauth" in joined:
                blocks.append(
                    "Id=mcp-oauth.service\nActiveState=active\nSubState=running\n"
                    "UnitFileState=enabled\nLoadState=loaded\n"
                )
            if "vibe-cli" in joined:
                blocks.append(
                    "Id=mcp-cli-server@vibe-cli.service\nActiveState=failed\nSubState=failed\n"
                    "UnitFileState=masked\nNRestarts=0\nLoadState=masked\n"
                )
            return "\n\n".join(blocks)

        runners = {
            "ss": lambda: "",
            "show": show,
            "show_system": lambda units: "Id=nginx.service\nActiveState=active\nSubState=running\nUnitFileState=enabled\n",
            "list_units": lambda: "mcp-cli-server@vibe-cli.service loaded failed failed leftover\n",
            "docker": lambda: "",
            "free": lambda: "Mem: 16000 4000 2000 0 1000 8000\nSwap: 2559 100 2459\n",
            "df": lambda: "Filesystem Size Used Avail Use% Mounted\n/dev/sda 30G 20G 8G 72% /\n",
            "watchdog": lambda: "",
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "servers.json").write_text("{}")
            data = collect(root, now=now, runners=runners)
        by = {s["name"]: s for s in data["services"]}
        self.assertTrue(by["vibe-cli"]["leftover"])
        self.assertEqual(by["vibe-cli"]["active"], "failed")

    def test_failed_nginx_core_pulse(self):
        now = dt.datetime(2026, 8, 18, 12, 0, 0, tzinfo=dt.timezone.utc)
        runners = {
            "ss": lambda: "",
            "show": lambda units: (
                "Id=mcp-oauth.service\nActiveState=active\nSubState=running\n"
                "UnitFileState=enabled\nLoadState=loaded\n\n"
                "Id=mcp-glama.service\nActiveState=active\nSubState=running\n"
                "UnitFileState=enabled\nLoadState=loaded\n\n"
                "Id=mcp-watchdog.timer\nActiveState=active\nSubState=waiting\n"
                "UnitFileState=enabled\nLoadState=loaded\n\n"
                "Id=mcp-status.service\nActiveState=active\nSubState=running\n"
                "UnitFileState=enabled\nLoadState=loaded\n"
            ),
            "show_system": lambda units: (
                "Id=nginx.service\nActiveState=failed\nSubState=failed\nUnitFileState=enabled\n"
            ),
            "list_units": lambda: "",
            "docker": lambda: "",
            "free": lambda: "Mem: 16000 4000 2000 0 1000 8000\nSwap: 2559 100 2459\n",
            "df": lambda: "Filesystem Size Used Avail Use% Mounted\n/dev/sda 30G 20G 8G 72% /\n",
            "watchdog": lambda: "",
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "servers.json").write_text("{}")
            data = collect(root, now=now, runners=runners)
        nginx = next(c for c in data["core"] if c["name"] == "nginx")
        self.assertEqual(nginx["severity"], "bad")
        self.assertTrue(nginx["pulse"])
        self.assertTrue(nginx["live"])
        self.assertGreaterEqual(data["summary"]["live"], 1)

    def test_host_issues_structured(self):
        now = dt.datetime(2026, 8, 18, 12, 0, 0, tzinfo=dt.timezone.utc)

        def _runners(free: str, df: str):
            return {
                "ss": lambda: "",
                "show": lambda units: (
                    "Id=mcp-oauth.service\nActiveState=active\nSubState=running\n"
                    "UnitFileState=enabled\nLoadState=loaded\n"
                ),
                "show_system": lambda units: (
                    "Id=nginx.service\nActiveState=active\nSubState=running\nUnitFileState=enabled\n"
                ),
                "list_units": lambda: "",
                "docker": lambda: "",
                "free": lambda: free,
                "df": lambda: df,
                "watchdog": lambda: "",
            }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "servers.json").write_text("{}")
            data = collect(
                root, now=now,
                runners=_runners(
                    "Mem: 16000 4000 2000 0 1000 400\nSwap: 2559 2555 4\n",
                    "Filesystem Size Used Avail Use% Mounted\n/dev/sda 30G 27G 2G 90% /\n",
                ),
            )
            warn_disk = collect(
                root, now=now,
                runners=_runners(
                    "Mem: 16000 4000 2000 0 1000 8000\nSwap: 2559 2555 4\n",
                    "Filesystem Size Used Avail Use% Mounted\n/dev/sda 30G 20G 7.8G 83% /\n",
                ),
            )
        by_msg = {i["message"]: i["severity"] for i in data["host"]["issues"]}
        self.assertEqual(by_msg.get("memory 400 MB available"), "bad")
        self.assertEqual(by_msg.get("disk 90% used (2G free)"), "bad")
        self.assertEqual(by_msg.get("swap 2555/2559 MB"), "warn")
        for item in data["host"]["issues"]:
            self.assertEqual(set(item), {"severity", "message"})
        svc_n = sum(1 for s in data["services"] if s["issues"])
        core_n = sum(1 for c in data["core"] if c["issues"])
        self.assertEqual(data["summary"]["issues"], svc_n + core_n + len(data["host"]["issues"]))
        warn_msgs = {i["message"]: i["severity"] for i in warn_disk["host"]["issues"]}
        self.assertEqual(warn_msgs.get("disk 83% used (7.8G free)"), "warn")
        self.assertEqual(warn_msgs.get("swap 2555/2559 MB"), "warn")

    def test_main_html_stamps_snapshot(self):
        fake = {
            "generated_at": "now",
            "host_url": "https://mcp.example.com",
            "summary": {
                "enabled": 0, "manifest": 0, "up": 0, "issues": 0,
                "restarting": 0, "restarts_total": 0, "live": 0,
            },
            "core": [],
            "services": [],
            "containers": [],
            "watchdog": {"last": "-", "heals": 0, "recent_heals": []},
            "host": {
                "mem_avail_mb": 2000, "swap_used_mb": 0, "swap_tot_mb": 1,
                "disk_used": "1%", "disk_free": "9G", "issues": [],
            },
        }
        import status_collect as sc
        orig = sc.collect
        sc.collect = lambda *a, **k: fake
        try:
            with tempfile.TemporaryDirectory() as td:
                path = Path(td) / "status.html"
                self.assertEqual(main(["--html", str(path)]), 0)
                self.assertEqual(fake.get("source"), "snapshot")
                self.assertTrue(path.is_file())
        finally:
            sc.collect = orig


class CollectProbe(unittest.TestCase):
    """The protocol-level probe: port up + MCP handshake broken must flag."""

    def _runner_base(self, show_text: str, ss_text: str):
        return {
            "ss": lambda: ss_text,
            "show": lambda units: show_text,
            "show_system": lambda units: (
                "Id=nginx.service\nActiveState=active\nSubState=running\nUnitFileState=enabled\n"
            ),
            "list_units": lambda: "",
            "docker": lambda: "",
            "free": lambda: "Mem: 16000 4000 2000 0 1000 8000\nSwap: 2559 100 2459\n",
            "df": lambda: "Filesystem Size Used Avail Use% Mounted\n/dev/sda 30G 20G 8G 72% /\n",
            "watchdog": lambda: "",
        }

    def _github_show(self) -> str:
        return (
            "Id=mcp-server@github.service\nActiveState=active\nSubState=running\n"
            "UnitFileState=enabled\nNRestarts=0\nActiveEnterTimestamp=Sun 2026-08-16 01:32:05 UTC\n"
            "LoadState=loaded\n"
        )

    def _run(self, fake: dict):
        import status_collect as sc
        now = dt.datetime(2026, 8, 18, 12, 0, 0, tzinfo=dt.timezone.utc)
        orig = sc.mcp_probe_http
        sc.mcp_probe_http = lambda port, timeout=sc.PROBE_TIMEOUT_SEC: dict(fake)
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "servers.json").write_text(json.dumps({
                    "github": {"port": 8812, "env": {}},
                }))
                data = collect(
                    root, now=now,
                    runners=self._runner_base(
                        self._github_show(),
                        "LISTEN 0 511 127.0.0.1:8812 0.0.0.0:*\n",
                    ),
                )
        finally:
            sc.mcp_probe_http = orig
        return data

    def test_handshake_failure_flags_up_bridge(self):
        data = self._run({"ok": False, "version": None, "protocol": None, "error": "HTTP 500"})
        gh = next(s for s in data["services"] if s["name"] == "github")
        self.assertTrue(gh["listening"])
        self.assertEqual(gh["severity"], "bad")
        self.assertTrue(gh["pulse"])
        self.assertTrue(any(i.startswith("MCP handshake failed") for i in gh["issues"]))
        self.assertIn("MCP handshake failed", render_text(data))
        self.assertGreaterEqual(data["summary"]["live"], 1)

    def test_handshake_ok_reports_engine_version(self):
        data = self._run({"ok": True, "version": "0.4.0", "protocol": "2025-11-25", "error": None})
        gh = next(s for s in data["services"] if s["name"] == "github")
        self.assertEqual(gh["severity"], "ok")
        self.assertFalse(gh["issues"])
        self.assertFalse(gh["pulse"])
        self.assertEqual(gh["proto"]["version"], "0.4.0")
        self.assertIn("MCP 0.4.0", render_text(data))

    def test_no_network_when_environ_injected(self):
        """Hermetic callers (environ={}) must never touch the loopback bridges."""
        import status_collect as sc
        now = dt.datetime(2026, 8, 18, 12, 0, 0, tzinfo=dt.timezone.utc)
        orig = sc.mcp_probe_http
        called = []
        sc.mcp_probe_http = lambda port, timeout=sc.PROBE_TIMEOUT_SEC: called.append(port) or {
            "ok": False, "version": None, "protocol": None, "error": "boom",
        }
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "servers.json").write_text(json.dumps({
                    "github": {"port": 8812, "env": {}},
                }))
                collect(
                    root, now=now,
                    runners=self._runner_base(
                        self._github_show(),
                        "LISTEN 0 511 127.0.0.1:8812 0.0.0.0:*\n",
                    ),
                    environ={},
                )
        finally:
            sc.mcp_probe_http = orig
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
