#!/usr/bin/env python3
"""RFC 9728 401 challenge + path-scoped PRM (Gemini Spark / Notion parity).

Unauthenticated POST /oauth/ex/<name>/mcp must be 401 JSON invalid_token with
WWW-Authenticate resource_metadata pointing at
/.well-known/oauth-protected-resource<request-uri>. nginx auth_request otherwise
emits HTML; the example config uses @mcp_oauth_401. This is discovery parity —
DCR / CIMD / grok stay in the example.
"""
from __future__ import annotations

import importlib.util
import json
import re
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "bin" / "oauth-server.py"
_spec = importlib.util.spec_from_file_location("oauth_server_401", _SRC)
oauth_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oauth_server)

ISSUER = "https://mcp.example.test"
STATIC_ID = "grok"
STATIC_SECRET = "test-static-secret-not-for-prod"
JWT_SECRET = "test-jwt-secret-not-for-prod"


def _conf():
    return {
        "client_id": STATIC_ID,
        "client_secret": STATIC_SECRET,
        "jwt_secret": JWT_SECRET,
        "issuer": ISSUER,
        "token_ttl": 3600,
        "refresh_ttl": 86400,
        "code_ttl": 300,
        "port": 0,
        "redirect_prefixes": ["https://app.example.test/"],
    }


class PrmHelperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        oauth_server.apply_conf(
            _conf(),
            clients_path=str(Path(self.tmp.name) / "oauth-clients.json"),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_prm_path_from_mcp_uri_and_well_known(self):
        mcp = "/oauth/ex/acpx-cli/mcp"
        self.assertEqual(oauth_server.prm_resource_path(mcp), mcp)
        self.assertEqual(
            oauth_server.prm_resource_path(
                oauth_server.PRM_WELL_KNOWN + mcp
            ),
            mcp,
        )
        self.assertEqual(oauth_server.prm_resource_path(oauth_server.PRM_WELL_KNOWN), "")
        self.assertEqual(
            oauth_server.prm_resource_path("/oauth/ex/openscad/mcp?x=1"),
            "/oauth/ex/openscad/mcp?x=1",
        )

    def test_prm_document_path_scoped(self):
        root = oauth_server.prm_document("")
        self.assertEqual(root["resource"], ISSUER)
        self.assertEqual(root["authorization_servers"], [ISSUER])
        self.assertEqual(root["scopes_supported"], ["mcp"])
        self.assertEqual(root["bearer_methods_supported"], ["header"])

        scoped = oauth_server.prm_document("/oauth/ex/acpx-cli/mcp")
        self.assertEqual(
            scoped["resource"],
            ISSUER + "/oauth/ex/acpx-cli/mcp",
        )

    def test_bearer_challenge_matches_success_shape(self):
        hdr = oauth_server.bearer_www_authenticate("/oauth/ex/acpx-cli/mcp")
        self.assertTrue(hdr.startswith('Bearer realm="OAuth"'))
        self.assertIn(
            'resource_metadata="https://mcp.example.test/.well-known/oauth-protected-resource/oauth/ex/acpx-cli/mcp"',
            hdr,
        )
        self.assertIn('error="invalid_token"', hdr)
        self.assertNotIn("error_description", hdr)


class OAuthValidate401Test(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        oauth_server.apply_conf(
            _conf(),
            clients_path=str(Path(self.tmp.name) / "oauth-clients.json"),
            register_limit=20,
            register_window=60,
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), oauth_server.H)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def _req(self, method, path, headers=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        raw = resp.read()
        try:
            data = json.loads(raw.decode()) if raw else None
        except ValueError:
            data = raw
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        return resp.status, data, hdrs

    def test_validate_missing_token_is_invalid_token_json(self):
        st, data, hdrs = self._req("GET", "/validate")
        self.assertEqual(st, 401)
        self.assertEqual(data, {"error": "invalid_token"})
        self.assertEqual(hdrs.get("content-type", "").split(";")[0], "application/json")
        # Challenge is emitted by nginx @mcp_oauth_401, not /validate.

    def test_validate_bad_bearer_same_json(self):
        st, data, hdrs = self._req(
            "GET",
            "/validate",
            headers={"Authorization": "Bearer not-a-jwt"},
        )
        self.assertEqual(st, 401)
        self.assertEqual(data, {"error": "invalid_token"})
        self.assertEqual(hdrs.get("content-type", "").split(";")[0], "application/json")

    def test_prm_http_path_scoped(self):
        st, data, _ = self._req(
            "GET",
            "/.well-known/oauth-protected-resource/oauth/ex/acpx-cli/mcp",
        )
        self.assertEqual(st, 200)
        self.assertEqual(
            data["resource"],
            ISSUER + "/oauth/ex/acpx-cli/mcp",
        )
        st, root, _ = self._req("GET", "/.well-known/oauth-protected-resource")
        self.assertEqual(st, 200)
        self.assertEqual(root["resource"], ISSUER)


class NginxExample401Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.example = (_ROOT / "nginx" / "mcp.conf.example").read_text()
        cls.add_server = (_ROOT / "bin" / "add-server.sh").read_text()
        cls.snippet = (_ROOT / "nginx" / "open-swarm-oracle.location.conf.snippet").read_text()
        cls.builder = (_ROOT / "webui" / "gateway-builder.html").read_text()
        cls.clients = (_ROOT / "docs" / "CLIENTS.md").read_text()
        cls.runbook = (_ROOT / "docs" / "RUNBOOK.md").read_text()

    def test_named_location_and_header_shape(self):
        text = self.example
        self.assertIn("location @mcp_oauth_401", text)
        self.assertIn('return 401 \'{"error":"invalid_token"}\'', text)
        self.assertIn('Bearer realm="OAuth"', text)
        self.assertIn(
            'resource_metadata="https://$host/.well-known/oauth-protected-resource$request_uri"',
            text,
        )
        self.assertIn('error="invalid_token"', text)
        self.assertIn("default_type application/json", text)

    def test_every_auth_request_has_error_page(self):
        blocks = re.findall(
            r"auth_request /oauth-validate;\n(.*)",
            self.example,
        )
        self.assertGreaterEqual(len(blocks), 16)
        for rest in blocks:
            self.assertTrue(
                rest.strip().startswith("error_page 401 = @mcp_oauth_401;"),
                rest,
            )

    def test_prm_location_unauthenticated(self):
        text = self.example
        self.assertIn("location ^~ /.well-known/oauth-protected-resource", text)
        self.assertIn('{"resource":"https://$host$prm_suffix"', text)
        prm = text.split("location ^~ /.well-known/oauth-protected-resource", 1)[1]
        prm = prm.split("location ", 1)[0]
        self.assertNotIn("auth_request", prm)

    def test_dcr_cimd_and_grok_untouched(self):
        text = self.example
        self.assertIn("location = /oauth/register", text)
        self.assertIn("http://127.0.0.1:8780/register", text)
        self.assertIn("location = /.well-known/oauth-authorization-server", text)
        grok = text.split("location /grok/", 1)[1].split("location ", 1)[0]
        self.assertNotIn("error_page 401", grok)
        self.assertNotIn("auth_request", grok)
        status = text.split("location = /status {", 1)[1].split("location ", 1)[0]
        self.assertNotIn("@mcp_oauth_401", status)
        self.assertNotIn("listen 8815", text)
        self.assertNotIn("listen 3100", text)

    def test_helpers_and_docs_mention_curl_check(self):
        self.assertIn("error_page 401 = @mcp_oauth_401", self.add_server)
        self.assertIn("error_page 401 = @mcp_oauth_401", self.snippet)
        self.assertIn("error_page 401 = @mcp_oauth_401", self.builder)
        self.assertIn("resource_metadata", self.clients)
        self.assertIn("@mcp_oauth_401", self.runbook)
        self.assertIn("oauth/ex/acpx-cli/mcp", self.clients)
        self.assertIn('{"error":"invalid_token"}', self.clients)


if __name__ == "__main__":
    unittest.main()
