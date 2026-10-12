#!/usr/bin/env python3
"""CIMD (Client ID Metadata Documents) tests for bin/oauth-server.py."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import socket
import threading
import unittest
import urllib.parse
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

_SRC = Path(__file__).resolve().parents[1] / "bin" / "oauth-server.py"
_spec = importlib.util.spec_from_file_location("oauth_server_cimd", _SRC)
oauth_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oauth_server)


STATIC_ID = "grok"
STATIC_SECRET = "test-static-secret-not-for-prod"
JWT_SECRET = "test-jwt-secret-not-for-prod"
ISSUER = "https://mcp.example.test"
REDIRECT = "https://app.example.test/oauth/callback"
CIMD_URL = "https://cimd.example.test/oauth/client.json"
PUBLIC_IP = "8.8.8.8"


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


def _cimd_doc(url=CIMD_URL, **overrides):
    doc = {
        "client_id": url,
        "client_name": "Spark CIMD",
        "redirect_uris": [REDIRECT],
        "token_endpoint_auth_method": "none",
    }
    doc.update(overrides)
    return doc


class CimdHelperTest(unittest.TestCase):
    def test_is_cimd_client_id_shape(self):
        self.assertTrue(oauth_server.is_cimd_client_id(CIMD_URL))
        self.assertTrue(oauth_server.is_cimd_client_id("https://example.com/"))
        self.assertFalse(oauth_server.is_cimd_client_id("https://example.com"))
        self.assertFalse(oauth_server.is_cimd_client_id("http://cimd.example.test/client.json"))
        self.assertFalse(oauth_server.is_cimd_client_id("https://user@cimd.example.test/x"))
        self.assertFalse(oauth_server.is_cimd_client_id("https://cimd.example.test/a/../b"))
        self.assertFalse(oauth_server.is_cimd_client_id("https://cimd.example.test/x#frag"))
        self.assertFalse(oauth_server.is_cimd_client_id("grok"))
        self.assertFalse(oauth_server.is_cimd_client_id("not-registered"))

    def test_blocked_ips(self):
        for addr in (
            "127.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.0.2",
            "169.254.1.1", "100.64.0.1", "::1", "fe80::1", "192.0.2.1",
        ):
            self.assertTrue(oauth_server.is_blocked_ip(addr), addr)
        self.assertFalse(oauth_server.is_blocked_ip(PUBLIC_IP))
        self.assertFalse(oauth_server.is_blocked_ip("1.1.1.1"))

    def test_parse_cache_ttl(self):
        self.assertEqual(oauth_server.parse_cache_ttl({"cache-control": "max-age=120"}), 120)
        self.assertEqual(oauth_server.parse_cache_ttl({"cache-control": "no-store"}), 0)
        self.assertEqual(oauth_server.parse_cache_ttl({"cache-control": "no-cache"}), 0)
        self.assertEqual(
            oauth_server.parse_cache_ttl({"cache-control": "max-age=100", "age": "40"}),
            60,
        )
        self.assertEqual(
            oauth_server.parse_cache_ttl({"cache-control": "max-age=99999"}),
            oauth_server.CIMD_MAX_TTL,
        )
        self.assertEqual(oauth_server.parse_cache_ttl({}), oauth_server.CIMD_DEFAULT_TTL)

    def test_validate_cimd_document(self):
        rec, err = oauth_server.validate_cimd_document(CIMD_URL, _cimd_doc())
        self.assertIsNone(err)
        self.assertTrue(rec["cimd"])
        self.assertEqual(rec["token_endpoint_auth_method"], "none")
        _rec, err = oauth_server.validate_cimd_document(
            CIMD_URL, _cimd_doc(client_id="https://other.example.test/x")
        )
        self.assertIsNotNone(err)
        _rec, err = oauth_server.validate_cimd_document(
            CIMD_URL, _cimd_doc(client_name="")
        )
        self.assertIsNotNone(err)
        _rec, err = oauth_server.validate_cimd_document(
            CIMD_URL, _cimd_doc(client_secret="nope")
        )
        self.assertIsNotNone(err)

    def test_read_http_response_content_length(self):
        server, client = socket.socketpair()
        try:
            body = b'{"ok":true}'
            resp = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Cache-Control: max-age=60\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"\r\n"
            ) + body
            server.sendall(resp)
            server.shutdown(socket.SHUT_WR)
            status, headers, got = oauth_server._read_http_response(client, 5120)
            self.assertEqual(status, 200)
            self.assertEqual(got, body)
            self.assertEqual(headers["cache-control"], "max-age=60")
        finally:
            server.close()
            client.close()

    def test_read_http_response_rejects_oversize(self):
        server, client = socket.socketpair()
        try:
            body = b"x" * 100
            resp = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Length: 100\r\n"
                b"\r\n"
            ) + body
            server.sendall(resp)
            server.shutdown(socket.SHUT_WR)
            with self.assertRaises(OSError):
                oauth_server._read_http_response(client, 16)
        finally:
            server.close()
            client.close()


class OAuthCIMDTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store_path = str(Path(self.tmp.name) / "oauth-clients.json")
        oauth_server.apply_conf(
            _conf(),
            clients_path=self.store_path,
            register_limit=20,
            register_window=60,
            cimd_limit=20,
            cimd_window=60,
        )
        self.fetches = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), oauth_server.H)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        oauth_server.set_cimd_test_hooks(None, None)
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def _install_ok(self, url=CIMD_URL, doc=None, cache_control="max-age=600",
                    extra_headers=None, status=200, raw=None, content_type="application/json"):
        payload = raw if raw is not None else json.dumps(doc or _cimd_doc(url)).encode()

        def fetch(got):
            self.fetches.append(got)
            headers = {"content-type": content_type, "cache-control": cache_control}
            if extra_headers:
                headers.update(extra_headers)
            return status, headers, payload

        oauth_server.set_cimd_test_hooks(fetch=fetch, resolve=lambda _h: [PUBLIC_IP])

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

    def _authorize(self, cid, redirect=REDIRECT, **extra):
        q = {
            "client_id": cid,
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": "mcp",
            "state": "xyz",
        }
        q.update(extra)
        if "code_challenge" not in q:
            verifier, challenge = _pkce_s256()
            q["code_challenge"] = challenge
            q["code_challenge_method"] = "S256"
        else:
            verifier = extra.get("_verifier")
        return self._json("GET", "/authorize?" + urllib.parse.urlencode(
            {k: v for k, v in q.items() if not k.startswith("_")}
        )), verifier

    def test_metadata_advertises_cimd_and_dcr(self):
        status, data, _loc, _ = self._json("GET", "/.well-known/oauth-authorization-server")
        self.assertEqual(status, 200)
        self.assertTrue(data["client_id_metadata_document_supported"])
        self.assertEqual(data["registration_endpoint"], ISSUER + "/oauth/register")
        self.assertIn("none", data["token_endpoint_auth_methods_supported"])

    def test_cimd_authorize_token_validate_refresh(self):
        self._install_ok()
        verifier, challenge = _pkce_s256()
        (st, _data, loc, _), _v = self._authorize(
            CIMD_URL, code_challenge=challenge, code_challenge_method="S256",
            _verifier=verifier,
        )
        self.assertEqual(st, 302, loc)
        self.assertEqual(self.fetches, [CIMD_URL])
        parsed = urllib.parse.urlparse(loc)
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", REDIRECT)
        qs = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(qs.get("state"), ["xyz"])
        code = qs["code"][0]
        st, tok, _loc, _ = self._json("POST", "/token", form={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": CIMD_URL,
            "code_verifier": verifier,
        })
        self.assertEqual(st, 200, tok)
        self.assertEqual(tok["token_type"], "Bearer")
        claims = _jwt_payload(tok["access_token"])
        self.assertEqual(claims["client_id"], CIMD_URL)
        self.assertEqual(claims["typ"], "access")
        st, _v, _loc, _ = self._json(
            "GET", "/validate", headers={"Authorization": "Bearer " + tok["access_token"]}
        )
        self.assertEqual(st, 200)
        st, refreshed, _loc, _ = self._json("POST", "/token", form={
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
            "client_id": CIMD_URL,
        })
        self.assertEqual(st, 200, refreshed)
        self.assertEqual(_jwt_payload(refreshed["access_token"])["client_id"], CIMD_URL)
        # CIMD is not persisted to the DCR store
        if Path(self.store_path).is_file():
            stored = json.loads(Path(self.store_path).read_text())
            self.assertNotIn(CIMD_URL, stored.get("clients") or {})

    def test_cimd_cache_respects_max_age(self):
        self._install_ok(cache_control="max-age=600")
        verifier, challenge = _pkce_s256()
        (st, _d, loc, _), _ = self._authorize(
            CIMD_URL, code_challenge=challenge, code_challenge_method="S256",
        )
        self.assertEqual(st, 302, loc)
        (st2, _d2, loc2, _), _ = self._authorize(
            CIMD_URL, code_challenge=challenge, code_challenge_method="S256",
        )
        self.assertEqual(st2, 302, loc2)
        self.assertEqual(self.fetches, [CIMD_URL])

    def test_cimd_no_store_not_cached(self):
        self._install_ok(cache_control="no-store")
        verifier, challenge = _pkce_s256()
        (st, _d, loc, _), _ = self._authorize(
            CIMD_URL, code_challenge=challenge, code_challenge_method="S256",
        )
        self.assertEqual(st, 302, loc)
        (st2, _d2, loc2, _), _ = self._authorize(
            CIMD_URL, code_challenge=challenge, code_challenge_method="S256",
        )
        self.assertEqual(st2, 302, loc2)
        self.assertEqual(self.fetches, [CIMD_URL, CIMD_URL])

    def test_cimd_invalid_json_not_cached(self):
        self._install_ok(raw=b"not-json {", cache_control="max-age=600")
        (st, data, _loc, _), _ = self._authorize(
            CIMD_URL, code_challenge="abc", code_challenge_method="S256",
        )
        self.assertEqual(st, 400)
        self.assertEqual(data["error"], "unauthorized_client")
        (st2, _d, _loc2, _), _ = self._authorize(
            CIMD_URL, code_challenge="abc", code_challenge_method="S256",
        )
        self.assertEqual(st2, 400)
        self.assertEqual(len(self.fetches), 2)

    def test_cimd_client_id_mismatch_rejected(self):
        self._install_ok(doc=_cimd_doc(client_id="https://other.example.test/x"))
        (st, data, _loc, _), _ = self._authorize(
            CIMD_URL, code_challenge="abc", code_challenge_method="S256",
        )
        self.assertEqual(st, 400)
        self.assertEqual(data["error"], "unauthorized_client")
        self.assertIn("client_id", data.get("error_description", ""))

    def test_cimd_missing_required_fields(self):
        self._install_ok(doc={"client_id": CIMD_URL, "redirect_uris": [REDIRECT]})
        (st, data, _loc, _), _ = self._authorize(
            CIMD_URL, code_challenge="abc", code_challenge_method="S256",
        )
        self.assertEqual(st, 400)
        self.assertIn("client_name", data.get("error_description", ""))

    def test_cimd_redirect_uri_must_be_in_list(self):
        self._install_ok()
        (st, data, _loc, _), _ = self._authorize(
            CIMD_URL,
            redirect="https://evil.example.test/cb",
            code_challenge="abc",
            code_challenge_method="S256",
        )
        self.assertEqual(st, 400)
        self.assertEqual(data["error"], "invalid_request")
        self.assertIn("redirect_uri", data.get("error_description", ""))

    def test_cimd_requires_pkce(self):
        self._install_ok()
        q = urllib.parse.urlencode({
            "client_id": CIMD_URL,
            "redirect_uri": REDIRECT,
            "response_type": "code",
        })
        st, data, _loc, _ = self._json("GET", "/authorize?" + q)
        self.assertEqual(st, 400)
        self.assertEqual(data["error"], "invalid_request")
        self.assertIn("PKCE", data.get("error_description", ""))

    def test_http_client_id_not_fetched(self):
        fetched = []

        def fetch(url):
            fetched.append(url)
            return 200, {"content-type": "application/json"}, b"{}"

        oauth_server.set_cimd_test_hooks(fetch=fetch, resolve=lambda _h: [PUBLIC_IP])
        q = urllib.parse.urlencode({
            "client_id": "http://cimd.example.test/client.json",
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "code_challenge": "abc",
            "code_challenge_method": "S256",
        })
        st, data, _loc, _ = self._json("GET", "/authorize?" + q)
        self.assertEqual(st, 400)
        self.assertEqual(data["error"], "unauthorized_client")
        self.assertEqual(fetched, [])

    def test_loopback_and_private_ips_blocked(self):
        fetched = []

        def fetch(url):
            fetched.append(url)
            return 200, {"content-type": "application/json"}, json.dumps(_cimd_doc(url)).encode()

        oauth_server.set_cimd_test_hooks(fetch=fetch, resolve=lambda _h: ["127.0.0.1"])
        for cid in (
            "https://127.0.0.1/client.json",
            "https://192.168.1.1/cimd",
            "https://10.1.2.3/meta",
            CIMD_URL,  # resolve hook returns loopback
        ):
            fetched.clear()
            q = urllib.parse.urlencode({
                "client_id": cid,
                "redirect_uri": REDIRECT,
                "response_type": "code",
                "code_challenge": "abc",
                "code_challenge_method": "S256",
            })
            st, data, _loc, _ = self._json("GET", "/authorize?" + q)
            self.assertEqual(st, 400, cid)
            self.assertEqual(data["error"], "unauthorized_client")
            self.assertEqual(fetched, [], cid)

    def test_cimd_fetch_rate_limit(self):
        oauth_server.CIMD_RATE = oauth_server.RateLimiter(limit=1, window=60)
        urls = [
            "https://cimd.example.test/one.json",
            "https://cimd.example.test/two.json",
        ]
        self._install_ok()

        def fetch(url):
            self.fetches.append(url)
            return 200, {
                "content-type": "application/json",
                "cache-control": "no-store",
            }, json.dumps(_cimd_doc(url)).encode()

        oauth_server.set_cimd_test_hooks(fetch=fetch, resolve=lambda _h: [PUBLIC_IP])
        (st, _d, loc, _), _ = self._authorize(
            urls[0], code_challenge="abc", code_challenge_method="S256",
        )
        self.assertEqual(st, 302, loc)
        (st2, data, _loc, resp), _ = self._authorize(
            urls[1], code_challenge="abc", code_challenge_method="S256",
        )
        self.assertEqual(st2, 429)
        self.assertEqual(data["error"], "slow_down")
        self.assertEqual(resp.getheader("Retry-After"), "60")
        self.assertEqual(self.fetches, [urls[0]])

    def test_cimd_does_not_break_dcr_or_grok(self):
        # DCR still registers and authorizes
        body = {
            "client_name": "Spark Test",
            "redirect_uris": [REDIRECT],
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        st, reg, _loc, _ = self._json("POST", "/register", body=body)
        self.assertEqual(st, 201, reg)
        cid = reg["client_id"]
        verifier, challenge = _pkce_s256()
        (st, _d, loc, _), _ = self._authorize(
            cid, code_challenge=challenge, code_challenge_method="S256",
        )
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
        # static grok secret flow
        q = urllib.parse.urlencode({
            "client_id": STATIC_ID,
            "redirect_uri": REDIRECT,
            "response_type": "code",
        })
        st, _d, loc, _ = self._json("GET", "/authorize?" + q)
        self.assertEqual(st, 302)
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

    def test_non_200_and_html_rejected(self):
        self._install_ok(status=302, extra_headers={"location": "https://evil.test/x"})
        (st, data, _loc, _), _ = self._authorize(
            CIMD_URL, code_challenge="abc", code_challenge_method="S256",
        )
        self.assertEqual(st, 400)
        self.assertEqual(data["error"], "unauthorized_client")

        self._install_ok(content_type="text/html", raw=b"<html>nope</html>")
        (st, data, _loc, _), _ = self._authorize(
            CIMD_URL, code_challenge="abc", code_challenge_method="S256",
        )
        self.assertEqual(st, 400)


if __name__ == "__main__":
    unittest.main()
