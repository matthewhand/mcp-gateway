#!/usr/bin/env python3
"""Localhost status WebUI for mcp-gateway.

Bind 127.0.0.1 only. nginx fronts / and /status with htpasswd.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from status_collect import collect, render_html  # noqa: E402

HOST = os.environ.get("MCP_STATUS_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_STATUS_PORT", "8781"))
ROOT = Path(__file__).resolve().parent.parent

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
HTML_PATHS = {"/", "/status", "/status.html"}
JSON_PATHS = {"/status.json", "/json"}

CACHE_TTL_SEC = 1.5
_cache_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "data": None}


def _safe_err(msg: str) -> None:
    try:
        sys.stderr.write(msg)
        sys.stderr.flush()
    except Exception:
        pass


def _snapshot() -> dict:
    """Collect under a lock so overlapping misses share one snapshot."""
    with _cache_lock:
        now = time.monotonic()
        if _cache["data"] is not None and (now - _cache["ts"]) < CACHE_TTL_SEC:
            return _cache["data"]
        data = collect(ROOT)
        data["source"] = os.environ.get("MCP_STATUS_SOURCE", "live")
        _cache["ts"] = time.monotonic()
        _cache["data"] = data
        return data


class Handler(BaseHTTPRequestHandler):
    server_version = "mcp-status/1.0"

    def log_message(self, fmt: str, *args) -> None:
        _safe_err("%s - %s\n" % (self.address_string(), fmt % args))

    def _headers(self, code: int, content_type: str, content_length: int | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        if content_length is not None:
            self.send_header("Content-Length", str(content_length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex")
        self.end_headers()

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self._headers(code, content_type, len(body))
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in HTML_PATHS:
            try:
                html = render_html(_snapshot())
            except Exception as e:
                html = f"<!doctype html><meta charset=utf-8><pre>status collect failed: {e}</pre>"
                self._send(500, html.encode(), "text/html; charset=utf-8")
                return
            self._send(200, html.encode(), "text/html; charset=utf-8")
            return
        if path in JSON_PATHS:
            try:
                payload = json.dumps(_snapshot(), indent=2).encode()
            except Exception as e:
                payload = json.dumps({"error": str(e)}).encode()
                self._send(500, payload, "application/json")
                return
            self._send(200, payload, "application/json")
            return
        self._send(404, b"not found\n", "text/plain; charset=utf-8")

    def do_HEAD(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in HTML_PATHS:
            self._headers(200, "text/html; charset=utf-8")
            return
        if path in JSON_PATHS:
            self._headers(200, "application/json")
            return
        self._headers(404, "text/plain; charset=utf-8")


def main() -> int:
    host = os.environ.get("MCP_STATUS_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_STATUS_PORT", "8781"))
    if host not in LOOPBACK_HOSTS and os.environ.get("MCP_STATUS_ALLOW_NONLOCAL") != "1":
        _safe_err(f"mcp-status refuses non-loopback bind {host} — use 127.0.0.1\n")
        return 2
    httpd = ThreadingHTTPServer((host, port), Handler)
    _safe_err(f"mcp-status listening on {host}:{port}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
