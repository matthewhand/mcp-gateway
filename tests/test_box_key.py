#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from box_key import REASON, box_key_status, jwt_exp_unix  # noqa: E402
from status_collect import classify, collect  # noqa: E402
import datetime as dt


def _jwt(exp: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"aaa.{payload}.sig"


class BoxKeyStatus(unittest.TestCase):
    def test_empty_is_unhealthy(self):
        gate = box_key_status(
            manifest={"box": {"port": 8810, "env": {"API_KEY": "${BOX_API_KEY}"}}},
            environ={},
            env_files=[],
            key_files=[],
        )
        self.assertTrue(gate["unhealthy"])
        self.assertFalse(gate["present"])
        self.assertEqual(gate["reason"], REASON)
        blob = json.dumps(gate)
        self.assertNotIn("BOXSECRET", blob)
        self.assertNotIn("${", blob)

    def test_env_present(self):
        gate = box_key_status(
            manifest={"box": {"port": 8810, "env": {"API_KEY": "${BOX_API_KEY}"}}},
            environ={"BOX_API_KEY": "not-a-secret-for-tests-ok"},
            env_files=[],
            key_files=[],
        )
        self.assertFalse(gate["unhealthy"])
        self.assertTrue(gate["present"])
        self.assertIsNone(gate["reason"])
        self.assertNotIn("not-a-secret", json.dumps(gate))

    def test_key_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "box-api-key"
            path.write_text("file-token-value\n")
            gate = box_key_status(
                manifest={"box": {"env": {"API_KEY": "${BOX_API_KEY}"}}},
                environ={},
                env_files=[],
                key_files=[path],
            )
        self.assertTrue(gate["present"])
        self.assertFalse(gate["unhealthy"])

    def test_expired_jwt(self):
        token = _jwt(1)
        gate = box_key_status(
            manifest={"box": {"env": {"API_KEY": "${BOX_API_KEY}"}}},
            environ={"BOX_API_KEY": token},
            env_files=[],
            key_files=[],
            now=int(time.time()),
        )
        self.assertTrue(gate["expired"])
        self.assertTrue(gate["unhealthy"])
        self.assertEqual(gate["reason"], REASON)
        self.assertNotIn(token, json.dumps(gate))

    def test_fresh_jwt(self):
        token = _jwt(int(time.time()) + 3600)
        gate = box_key_status(
            manifest={"box": {"env": {"API_KEY": "${BOX_API_KEY}"}}},
            environ={"BOX_API_KEY": token},
            env_files=[],
            key_files=[],
            now=int(time.time()),
        )
        self.assertFalse(gate["expired"])
        self.assertFalse(gate["unhealthy"])

    def test_jwt_exp_unix_ignores_opaque(self):
        self.assertIsNone(jwt_exp_unix("opaque-token"))
        self.assertIsNone(jwt_exp_unix(""))

    def test_unhealthy_means_check_fails(self):
        gate = box_key_status(
            manifest={"box": {"port": 8810, "env": {"API_KEY": "${BOX_API_KEY}"}}},
            environ={},
            env_files=[],
            key_files=[],
        )
        self.assertTrue(gate["unhealthy"])
        self.assertEqual(1 if gate["unhealthy"] else 0, 1)


class BoxClassifyAndCollect(unittest.TestCase):
    def test_manual_off_with_key_still_ok(self):
        sev, issues = classify({
            "expected": True, "enabled": False, "manual": True, "leftover": False,
            "listening": False, "port": 8810, "active": "inactive", "sub": "dead",
            "nrestarts": 0, "uptime_sec": None, "load": "loaded",
        })
        self.assertEqual(sev, "ok")
        self.assertEqual(issues, [])

    def test_manual_off_empty_key_unhealthy(self):
        sev, issues = classify({
            "expected": True, "enabled": False, "manual": True, "leftover": False,
            "listening": False, "port": 8810, "active": "inactive", "sub": "dead",
            "nrestarts": 0, "uptime_sec": None, "load": "loaded",
            "key_unhealthy": True,
            "key_reason": REASON,
        })
        self.assertEqual(sev, "warn")
        self.assertIn(REASON, issues)

    def test_active_hold_without_key_not_port_crash(self):
        sev, issues = classify({
            "expected": True, "enabled": False, "manual": True, "leftover": False,
            "listening": False, "port": 8810, "active": "active", "sub": "running",
            "nrestarts": 0, "uptime_sec": 30, "load": "loaded",
            "key_unhealthy": True,
            "key_reason": REASON,
        })
        self.assertEqual(sev, "warn")
        self.assertTrue(any("refresh BOX_API_KEY" in i for i in issues))
        self.assertFalse(any(i.startswith("port ") for i in issues))

    def test_collect_marks_unhealthy_without_leaking(self):
        now = dt.datetime(2026, 8, 18, 12, 0, 0, tzinfo=dt.timezone.utc)
        runners = {
            "ss": lambda: "",
            "show": lambda units: (
                "Id=mcp-server@box.service\nActiveState=inactive\nSubState=dead\n"
                "UnitFileState=disabled\nNRestarts=0\nLoadState=loaded\n"
            ),
            "show_system": lambda units: (
                "Id=nginx.service\nActiveState=active\nSubState=running\nUnitFileState=enabled\n"
            ),
            "list_units": lambda: "",
            "docker": lambda: "",
            "free": lambda: "Mem: 16000 4000 2000 0 1000 8000\nSwap: 2559 100 2459\n",
            "df": lambda: "Filesystem Size Used Avail Use% Mounted\n/dev/sda 30G 20G 8G 72% /\n",
            "watchdog": lambda: "",
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "servers.json").write_text(json.dumps({
                "box": {"port": 8810, "env": {"API_KEY": "${BOX_API_KEY}"}},
            }))
            data = collect(root, now=now, runners=runners, environ={})
        by = {s["name"]: s for s in data["services"]}
        self.assertTrue(by["box"]["key_unhealthy"])
        self.assertEqual(by["box"]["severity"], "warn")
        self.assertIn(REASON, by["box"]["issues"])
        self.assertFalse(by["box"]["listening"])
        blob = json.dumps(data)
        self.assertNotIn("BOXSECRET", blob)
        self.assertNotIn("${BOX_API_KEY}", blob)


if __name__ == "__main__":
    unittest.main()
