#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import status_web  # noqa: E402


def _payload() -> dict:
    return {
        "generated_at": "now",
        "host_url": "https://mcp.example.com",
        "schema_version": 1,
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


class TestStatusWeb(unittest.TestCase):
    def setUp(self):
        self.calls: list = []
        self._orig = status_web.collect
        self.data = _payload()

        def fake_collect(root=None):
            self.calls.append(root)
            return dict(self.data)

        status_web.collect = fake_collect
        with status_web._cache_lock:
            status_web._cache["ts"] = 0.0
            status_web._cache["data"] = None

    def tearDown(self):
        status_web.collect = self._orig
        with status_web._cache_lock:
            status_web._cache["ts"] = 0.0
            status_web._cache["data"] = None

    def _serve(self) -> ThreadingHTTPServer:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), status_web.Handler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        return httpd

    def test_head_does_not_collect(self):
        httpd = self._serve()
        try:
            port = httpd.server_address[1]
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            for path, ctype in (
                ("/", "text/html"),
                ("/status", "text/html"),
                ("/status.html", "text/html"),
                ("/status.json", "application/json"),
                ("/json", "application/json"),
            ):
                conn.request("HEAD", path)
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200, path)
                self.assertIn(ctype, resp.getheader("Content-Type") or "")
                self.assertEqual(resp.read(), b"")
            conn.request("HEAD", "/missing")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 404)
            self.assertIn("text/plain", resp.getheader("Content-Type") or "")
            self.assertEqual(resp.read(), b"")
            self.assertEqual(self.calls, [])
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_unknown_path_404_without_collect(self):
        httpd = self._serve()
        try:
            port = httpd.server_address[1]
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/nope")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 404)
            self.assertEqual(resp.read(), b"not found\n")
            self.assertEqual(self.calls, [])
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_non_loopback_main_exits_2(self):
        old_allow = os.environ.pop("MCP_STATUS_ALLOW_NONLOCAL", None)
        try:
            with patch.dict(os.environ, {"MCP_STATUS_HOST": "0.0.0.0"}):
                self.assertEqual(status_web.main(), 2)
            with patch.dict(os.environ, {"MCP_STATUS_HOST": "192.168.1.10"}):
                self.assertEqual(status_web.main(), 2)
        finally:
            if old_allow is not None:
                os.environ["MCP_STATUS_ALLOW_NONLOCAL"] = old_allow
        self.assertEqual(self.calls, [])

    def test_cache_single_flight_and_ttl(self):
        self.assertEqual(status_web.CACHE_TTL_SEC, 1.5)
        entered = threading.Event()
        release = threading.Event()

        def slow(root=None):
            self.calls.append(root)
            entered.set()
            self.assertTrue(release.wait(2))
            return dict(self.data)

        status_web.collect = slow
        results: list = []
        errors: list = []

        def go():
            try:
                results.append(status_web._snapshot())
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=go)
        t2 = threading.Thread(target=go)
        t1.start()
        self.assertTrue(entered.wait(1))
        t2.start()
        time.sleep(0.05)
        self.assertEqual(len(self.calls), 1)
        release.set()
        t1.join(2)
        t2.join(2)
        self.assertEqual(errors, [])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].get("source"), "live")

        status_web._snapshot()
        self.assertEqual(len(self.calls), 1)

        with status_web._cache_lock:
            status_web._cache["ts"] = time.monotonic() - status_web.CACHE_TTL_SEC - 0.01
        status_web.collect = lambda root=None: self.calls.append(root) or dict(self.data)
        status_web._snapshot()
        self.assertEqual(len(self.calls), 2)


if __name__ == "__main__":
    unittest.main()
