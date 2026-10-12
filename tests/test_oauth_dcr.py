#!/usr/bin/env python3
"""DCR (RFC 7591) + authorize/token tests for bin/oauth-server.py.

CIMD is implemented alongside DCR; metadata must advertise both.
Detailed CIMD fetch/cache/SSRF tests live in test_oauth_cimd.py.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import threading
import unittest
import urllib.parse
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

_SRC = Path(__file__).resolve().parents[1] / "bin" / "oauth-server.py"
_spec = importlib.util.spec_from_file_location("oauth_server", _SRC)
oauth_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oauth_server)


STATIC_ID = "grok"
STATIC_SECRET = "test-static-secret-not-for-prod"
JWT_SECRET = "test-jwt-secret-not-for-prod"
ISSUER = "https://mcp.example.test"
REDIRECT = "https://app.example.test/oauth/callback"
NATIVE_REDIRECT = "com.example.app:/oauth"


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


def _pkce_s256():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    challenge = oauth_server.b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def _jwt_payload(token: str) -> dict:
    _h, p, _s = token.split(".")
    return json.loads(oauth_server.b64url_dec(p))


class OAuthDCRTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store_path = str(Path(self.tmp.name) / "oauth-clients.json")
        oauth_server.apply_conf(
            _conf(),
            clients_path=self.store_path,
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

    def _conn(self) -> HTTPConnection:
        return HTTPConnection("127.0.0.1", self.port, timeout=5)

    def _json(self, method, path, body=None, headers=None, form=None):
        hdrs = dict(headers or {})
        payload = None
        if form is not None:
            payload = urllib.parse.urlencode(form)
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif body is not None:
            payload = json.dumps(body)
            hdrs.setdefault("Content-Type", "application/json")
        conn = self._conn()
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        loc = resp.getheader("Location")
        try:
            data = json.loads(raw.decode()) if raw else None
        except ValueError:
            data = raw
        conn.close()
        return resp.status, data, loc, resp

    def _register(self, **overrides):
        body = {
            "client_name": "Spark Test",
            "redirect_uris": [REDIRECT],
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "application_type": "web",
        }
        body.update(overrides)
        return self._json("POST", "/register", body=body)

    def test_metadata_has_registration_endpoint_and_cimd(self):
        status, data, _loc, _ = self._json("GET", "/.well-known/oauth-authorization-server")
        self.assertEqual(status, 200)
        self.assertEqual(data["issuer"], ISSUER)
        self.assertEqual(data["registration_endpoint"], ISSUER + "/oauth/register")
        self.assertEqual(data["authorization_endpoint"], ISSUER + "/oauth/authorize")
        self.assertEqual(data["token_endpoint"], ISSUER + "/oauth/token")
        self.assertTrue(data["client_id_metadata_document_supported"])
        self.assertIn("none", data["token_endpoint_auth_methods_supported"])
        self.assertIn("client_secret_basic", data["token_endpoint_auth_methods_supported"])
        self.assertIn("client_secret_post", data["token_endpoint_auth_methods_supported"])
        meta = oauth_server.as_metadata()
        self.assertEqual(meta["registration_endpoint"], ISSUER + "/oauth/register")
        self.assertTrue(meta["client_id_metadata_document_supported"])

    def test_register_public_client_no_secret(self):
        status, data, _loc, _ = self._register()
        self.assertEqual(status, 201, data)
        self.assertTrue(data["client_id"])
        self.assertNotEqual(data["client_id"], STATIC_ID)
        self.assertNotIn("client_secret", data)
        self.assertEqual(data["redirect_uris"], [REDIRECT])
        self.assertEqual(data["token_endpoint_auth_method"], "none")
        self.assertEqual(data["grant_types"], ["authorization_code"])
        self.assertEqual(data["response_types"], ["code"])
        self.assertIn("client_id_issued_at", data)
        stored = json.loads(Path(self.store_path).read_text())
        self.assertIn(data["client_id"], stored["clients"])
        self.assertNotIn("client_secret", stored["clients"][data["client_id"]])
        mode = Path(self.store_path).stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_register_rejects_wildcards_and_empty(self):
        for uris, _why in (
            ([], "empty list"),
            ([""], "empty string"),
            (["https://*.example.test/cb"], "wildcard host"),
            (["https://app.example.test/*"], "wildcard path"),
            (["not-a-uri"], "relative"),
            (["javascript:alert(1)"], "javascript"),
        ):
            status, data, _loc, _ = self._register(redirect_uris=uris)
            self.assertEqual(status, 400, (uris, data))
            self.assertEqual(data["error"], "invalid_redirect_uri")

    def test_register_rejects_non_none_auth_method(self):
        status, data, _loc, _ = self._register(token_endpoint_auth_method="client_secret_basic")
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "invalid_client_metadata")

    def test_register_rate_limit(self):
        oauth_server.RATE = oauth_server.RateLimiter(limit=2, window=60)
        self.assertEqual(self._register()[0], 201)
        self.assertEqual(self._register()[0], 201)
        status, data, _loc, resp = self._register()
        self.assertEqual(status, 429)
        self.assertEqual(data["error"], "slow_down")
        self.assertEqual(resp.getheader("Retry-After"), "60")

    def test_dcr_register_authorize_token_pkce(self):
        status, reg, _loc, _ = self._register()
        self.assertEqual(status, 201)
        cid = reg["client_id"]
        verifier, challenge = _pkce_s256()
        q = urllib.parse.urlencode({
            "client_id": cid,
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "mcp",
            "state": "xyz",
        })
        st, _data, loc, _ = self._json("GET", "/authorize?" + q)
        self.assertEqual(st, 302)
        parsed = urllib.parse.urlparse(loc)
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", REDIRECT)
        qs = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(qs.get("state"), ["xyz"])
        code = qs["code"][0]
        st, tok, _loc, _ = self._json("POST", "/token", form={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": cid,
            "code_verifier": verifier,
        })
        self.assertEqual(st, 200, tok)
        self.assertEqual(tok["token_type"], "Bearer")
        claims = _jwt_payload(tok["access_token"])
        self.assertEqual(claims["client_id"], cid)
        self.assertEqual(claims["typ"], "access")
        self.assertNotEqual(cid, STATIC_ID)
        # validate accepts the access token
        st, _v, _loc, _ = self._json(
            "GET", "/validate", headers={"Authorization": "Bearer " + tok["access_token"]}
        )
        self.assertEqual(st, 200)
        # refresh without secret (public DCR)
        st, refreshed, _loc, _ = self._json("POST", "/token", form={
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
            "client_id": cid,
        })
        self.assertEqual(st, 200, refreshed)
        self.assertEqual(_jwt_payload(refreshed["access_token"])["client_id"], cid)

    def test_dcr_requires_pkce_and_exact_redirect(self):
        _st, reg, _loc, _ = self._register()
        cid = reg["client_id"]
        q = urllib.parse.urlencode({
            "client_id": cid,
            "redirect_uri": REDIRECT,
            "response_type": "code",
        })
        st, data, _loc, _ = self._json("GET", "/authorize?" + q)
        self.assertEqual(st, 400)
        self.assertEqual(data["error"], "invalid_request")

        other = "https://evil.example.test/cb"
        q = urllib.parse.urlencode({
            "client_id": cid,
            "redirect_uri": other,
            "response_type": "code",
            "code_challenge": "abc",
            "code_challenge_method": "S256",
        })
        st, data, _loc, _ = self._json("GET", "/authorize?" + q)
        self.assertEqual(st, 400)
        self.assertIn("redirect_uri", data.get("error_description", ""))

    def test_dcr_unknown_client_rejected(self):
        q = urllib.parse.urlencode({
            "client_id": "not-registered",
            "redirect_uri": REDIRECT,
            "response_type": "code",
        })
        st, data, _loc, _ = self._json("GET", "/authorize?" + q)
        self.assertEqual(st, 400)
        self.assertEqual(data["error"], "unauthorized_client")

    def test_static_grok_secret_flow_still_works(self):
        q = urllib.parse.urlencode({
            "client_id": STATIC_ID,
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "scope": "mcp",
        })
        st, _data, loc, _ = self._json("GET", "/authorize?" + q)
        self.assertEqual(st, 302, loc)
        code = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)["code"][0]
        st, tok, _loc, _ = self._json("POST", "/token", form={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": STATIC_ID,
            "client_secret": STATIC_SECRET,
        })
        self.assertEqual(st, 200, tok)
        self.assertEqual(_jwt_payload(tok["access_token"])["client_id"], STATIC_ID)
        # refresh still requires the static secret
        st, _bad, _loc, _ = self._json("POST", "/token", form={
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
            "client_id": STATIC_ID,
        })
        self.assertEqual(st, 401)
        st, refreshed, _loc, _ = self._json("POST", "/token", form={
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
            "client_id": STATIC_ID,
            "client_secret": STATIC_SECRET,
        })
        self.assertEqual(st, 200, refreshed)

    def test_static_grok_wrong_secret_rejected(self):
        q = urllib.parse.urlencode({
            "client_id": STATIC_ID,
            "redirect_uri": REDIRECT,
            "response_type": "code",
        })
        st, _data, loc, _ = self._json("GET", "/authorize?" + q)
        code = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)["code"][0]
        st, data, _loc, _ = self._json("POST", "/token", form={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": STATIC_ID,
            "client_secret": "wrong-secret",
        })
        self.assertEqual(st, 401)
        self.assertEqual(data["error"], "invalid_client")

    def test_dcr_persists_across_reload(self):
        st, reg, _loc, _ = self._register()
        self.assertEqual(st, 201)
        cid = reg["client_id"]
        oauth_server.apply_conf(
            _conf(),
            clients_path=self.store_path,
            register_limit=20,
            register_window=60,
        )
        self.assertIsNotNone(oauth_server.lookup_client(cid))
        verifier, challenge = _pkce_s256()
        q = urllib.parse.urlencode({
            "client_id": cid,
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        st, _data, loc, _ = self._json("GET", "/authorize?" + q)
        self.assertEqual(st, 302)
        code = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)["code"][0]
        st, tok, _loc, _ = self._json("POST", "/token", form={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": cid,
            "code_verifier": verifier,
        })
        self.assertEqual(st, 200, tok)

    def test_native_custom_scheme_redirect(self):
        st, reg, _loc, _ = self._register(
            application_type="native",
            redirect_uris=[NATIVE_REDIRECT],
        )
        self.assertEqual(st, 201, reg)
        verifier, challenge = _pkce_s256()
        q = urllib.parse.urlencode({
            "client_id": reg["client_id"],
            "redirect_uri": NATIVE_REDIRECT,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        st, _data, loc, _ = self._json("GET", "/authorize?" + q)
        self.assertEqual(st, 302)
        self.assertTrue(loc.startswith(NATIVE_REDIRECT + "?"))

    def test_web_rejects_custom_scheme(self):
        st, data, _loc, _ = self._register(
            application_type="web",
            redirect_uris=[NATIVE_REDIRECT],
        )
        self.assertEqual(st, 400)
        self.assertEqual(data["error"], "invalid_redirect_uri")

    def test_nginx_example_exposes_register(self):
        example = Path(__file__).resolve().parents[1] / "nginx" / "mcp.conf.example"
        text = example.read_text()
        self.assertIn("location = /oauth/register", text)
        self.assertIn("http://127.0.0.1:8780/register", text)


class RedirectValidationTest(unittest.TestCase):
    def test_exact_list_helpers(self):
        self.assertIsNone(oauth_server.validate_redirect_uris([REDIRECT], "web"))
        self.assertIsNone(oauth_server.validate_redirect_uris(["http://localhost/cb"], "web"))
        self.assertIsNotNone(oauth_server.validate_redirect_uris(["http://evil.test/cb"], "web"))
        self.assertIsNotNone(oauth_server.validate_redirect_uris(["https://*.test/cb"], "web"))
        self.assertIsNotNone(oauth_server.validate_redirect_uris([], "web"))


if __name__ == "__main__":
    unittest.main()
