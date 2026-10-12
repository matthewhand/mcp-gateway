#!/usr/bin/env python3
"""Pin the P1 env-ref hardening: servers.json carries ${VAR} refs, never literals.

The tracked example manifest must stay clean, and the rule engine must flag a
literal key the moment one sneaks in (future live-manifest regressions included —
`bin/validate.sh` runs the same check against the real servers.json on the host).
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from secret_hygiene import check_manifest_secrets  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class ExampleManifest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((ROOT / "servers.example.json").read_text(encoding="utf-8"))

    def test_example_manifest_is_env_ref_clean(self):
        violations = check_manifest_secrets(self.manifest)
        self.assertEqual(violations, [])

    def test_every_secret_key_in_example_is_a_ref(self):
        for name, cfg in self.manifest.items():
            for key, raw in (cfg.get("env") or {}).items():
                value = str(raw)
                if key.endswith("API_KEY") and value:
                    self.assertTrue(
                        value.startswith("${") and value.endswith("}"),
                        f"{name}.env.{key} must be a ${{VAR}} ref, got: {value!r}",
                    )


class HygieneRule(unittest.TestCase):
    def test_ref_api_key_is_clean(self):
        m = {"svc": {"env": {"API_KEY": "${SVC_API_KEY}"}}}
        self.assertEqual(check_manifest_secrets(m), [])

    def test_empty_secret_key_is_clean(self):
        # box is manual + key-gated; an empty ref is the documented state.
        m = {"box": {"env": {"API_KEY": ""}}}
        self.assertEqual(check_manifest_secrets(m), [])

    def test_literal_api_key_is_violation(self):
        m = {"svc": {"env": {"API_KEY": "sk-literal-value-123"}}}
        violations = check_manifest_secrets(m)
        self.assertEqual(len(violations), 1)
        self.assertIn("svc.env.API_KEY", violations[0])
        self.assertIn("${API_KEY}", violations[0])

    def test_embedded_token_in_headers_is_violation(self):
        m = {"svc": {"env": {"EXTRA_HEADERS": "Authorization: Api-Key sk-abcdefghijklmnopqrstuvwxyz"}}}
        violations = check_manifest_secrets(m)
        self.assertEqual(len(violations), 1)
        self.assertIn("svc.env.EXTRA_HEADERS", violations[0])

    def test_ref_inside_extra_headers_is_clean(self):
        # getzep pattern: the proxy workaround header embeds a ${VAR} ref.
        m = {"getzep": {"env": {"EXTRA_HEADERS": "Authorization: Api-Key ${GETZEP_API_KEY}"}}}
        self.assertEqual(check_manifest_secrets(m), [])

    def test_auth_type_selectors_are_clean(self):
        m = {
            "netbox": {"env": {"API_AUTH_TYPE": "Token"}},
            "virustotal": {"env": {"API_AUTH_TYPE": "api-key", "API_AUTH_HEADER": "x-apikey"}},
            "wolframalpha": {"env": {"API_AUTH_TYPE": "query", "API_AUTH_PARAM": "appid"}},
            "gpt-terminal-plus": {"env": {"API_AUTH_TYPE": "Bearer"}},
        }
        self.assertEqual(check_manifest_secrets(m), [])

    def test_whitelist_and_url_values_are_clean(self):
        m = {
            "netbox": {
                "env": {
                    "TOOL_WHITELIST": "/api/dcim/devices,/api/ipam/ip-addresses,/api/ipam/prefixes",
                    "SERVER_URL_OVERRIDE": "http://localhost:8000",
                    "OPENAPI_SPEC_FORMAT": "yaml",
                }
            }
        }
        self.assertEqual(check_manifest_secrets(m), [])

    def test_short_literal_values_are_clean(self):
        # Short values (prefixes, headers like "2022-06-28") are not secrets.
        m = {
            "slack": {"env": {"STRIP_PARAM": "token", "TOOL_NAME_PREFIX": "slack_"}},
            "notion": {"env": {"EXTRA_HEADERS": "Notion-Version: 2022-06-28"}},
        }
        self.assertEqual(check_manifest_secrets(m), [])

    def test_uuid_style_literal_is_violation(self):
        # Hyphenated 36-char literals must still be caught by the bare-token sniff.
        m = {"svc": {"env": {"EXTRA_HEADERS": "X-Key: 550e8400-e29b-41d4-a716-446655440000"}}}
        violations = check_manifest_secrets(m)
        self.assertEqual(len(violations), 1)
        self.assertIn("possible embedded secret literal", violations[0])

    def test_jwt_style_segment_in_headers_is_violation(self):
        # Real JWT/base64url segments are 30+ chars and must be caught.
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        m = {"svc": {"env": {"EXTRA_HEADERS": f"Authorization: Bearer {jwt}"}}}
        violations = check_manifest_secrets(m)
        self.assertEqual(len(violations), 1)
        self.assertIn("possible embedded secret literal", violations[0])


if __name__ == "__main__":
    unittest.main()