#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from status_collect import collect, render_html  # noqa: E402


def _live_shape_data() -> dict:
    """Same runners/manifest as CollectLiveShape.test_collect_sorts_issues_and_hides_secrets."""
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
        }))
        return collect(root, now=now, runners=runners, environ={})


def _svc_row(html: str, name: str) -> str:
    rows = re.findall(r'<tr class="svc-row[^"]*"[^>]*>.*?</tr>', html, flags=re.S)
    needle = f"<b>{name}</b>"
    for row in rows:
        if needle in row:
            return row
    raise AssertionError(f"no svc-row for {name!r}")


def _empty_page(**overrides) -> dict:
    data = {
        "generated_at": "2026-08-18 12:00:00 UTC",
        "host_url": "https://mcp.example.com",
        "source": "live",
        "summary": {
            "enabled": 0, "manifest": 0, "up": 0, "issues": 0,
            "live": 0, "restarting": 0, "restarts_total": 0,
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
    data.update(overrides)
    return data


class RenderHtmlContract(unittest.TestCase):
    def test_live_fixture_pulse_and_chrome(self):
        data = _live_shape_data()
        html = render_html(data)
        openscad = _svc_row(html, "openscad")
        self.assertNotIn("svc-row--pulse", openscad)
        apisguru = _svc_row(html, "apisguru")
        self.assertIn("svc-row--pulse", apisguru)
        self.assertIn('id="app"', html)
        self.assertIn('th scope="row"', html)
        self.assertIn("prefers-reduced-motion", html)
        self.assertIn("Happening now", html)
        self.assertIn("issue-strip--bad", html)
        self.assertIn('href="#svc-apisguru"', html)
        self.assertNotIn("cli · cli", html)
        self.assertNotIn("SUPER-SECRET", html)
        self.assertNotIn("SUPER-SECRET-DO-NOT-LEAK", html)
        self.assertNotIn("spec_url", html)
        self.assertNotIn("BOXSECRET", html)
        self.assertNotIn("FAKE-EXTRA-HEADERS-DO-NOT-LEAK", html)

    def test_all_clear_when_no_issues(self):
        html = render_html(_empty_page())
        self.assertIn("All clear — no issues.", html)
        self.assertNotIn("No issues detected.", html)

    def test_host_issues_appear_as_strip_lines(self):
        data = _empty_page()
        data["host"] = {
            "mem_avail_mb": 1681, "swap_used_mb": 2555, "swap_tot_mb": 2559,
            "disk_used": "83%", "disk_free": "7.8G",
            "issues": [
                {"severity": "warn", "message": "disk 83% used (7.8G free)"},
                {"severity": "warn", "message": "swap 2555/2559 MB"},
            ],
        }
        data["summary"] = {
            "enabled": 0, "manifest": 0, "up": 0, "issues": 2,
            "live": 0, "restarting": 0, "restarts_total": 0,
        }
        html = render_html(data)
        self.assertIn("disk 83% used (7.8G free)", html)
        self.assertIn("swap 2555/2559 MB", html)
        self.assertIn("<b>host</b>", html)
        self.assertNotIn("All clear — no issues.", html)
        self.assertIn("issue-strip--warn", html)

    def test_pulse_class_only_from_row_flag(self):
        data = _empty_page()
        data["services"] = [{
            "name": "stale-flap",
            "kind": "openapi",
            "badge": "keyed",
            "enabled": True,
            "unit_file_state": "enabled",
            "active": "active",
            "sub": "running",
            "uptime": "20h 0m",
            "uptime_sec": 72000,
            "nrestarts": 10,
            "port": 8808,
            "listening": True,
            "severity": "bad",
            "issues": ["flapping — 10 automatic restarts"],
            "expected": True,
            "manual": False,
            "leftover": False,
            "pulse": False,
        }]
        data["summary"] = {
            "enabled": 1, "manifest": 1, "up": 1, "issues": 1,
            "live": 0, "restarting": 0, "restarts_total": 10,
        }
        html = render_html(data)
        row = _svc_row(html, "stale-flap")
        self.assertNotIn("svc-row--pulse", row)
        self.assertIn("svc-row--bad", row)
        self.assertIn("flapping — 10 automatic restarts", row)
        self.assertIn('class="issue-strip issue-strip--warn"', html)
        self.assertNotIn('class="issue-strip issue-strip--bad"', html)
        self.assertIn("Restart debt", html)
        self.assertIn('href="#svc-stale-flap"', html)
        self.assertIn('id="svc-stale-flap"', html)
        self.assertIn("Nothing is failing right now", html)


if __name__ == "__main__":
    unittest.main()
