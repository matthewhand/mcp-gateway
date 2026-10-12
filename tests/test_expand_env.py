#!/usr/bin/env python3
"""Pin the servers.json reference expansion: ${VAR} and ${VAR:-default}.

`bin/launch-server.sh` (and the CLI launchers) expand the manifest through
`bin/expand_env.py`. Plain `${VAR}` must behave exactly as the old inline
snippets did — value when set, literal when unset — while `${VAR:-default}`
must fall back to `default` when the var is unset or empty. No network, no
services: everything here is offline string work.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from expand_env import expand, expand_json, split_ref  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXPANDER = ROOT / "bin" / "expand_env.py"


class DefaultForm(unittest.TestCase):
    def test_unset_var_uses_default(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(
                expand("${GPT_TERMINAL_PLUS_URL:-http://localhost:3100}"),
                "http://localhost:3100",
            )

    def test_set_var_wins_over_default(self):
        with mock.patch.dict("os.environ", {"GPT_TERMINAL_PLUS_URL": "http://localhost:4100"}):
            self.assertEqual(
                expand("${GPT_TERMINAL_PLUS_URL:-http://localhost:3100}"),
                "http://localhost:4100",
            )

    def test_empty_var_uses_default(self):
        with mock.patch.dict("os.environ", {"GPT_TERMINAL_PLUS_URL": ""}):
            self.assertEqual(
                expand("${GPT_TERMINAL_PLUS_URL:-http://localhost:3100}"),
                "http://localhost:3100",
            )

    def test_default_may_contain_colon_and_slash(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(
                expand("${SERVER_URL:-http://127.0.0.1:3100}/openapi.json"),
                "http://127.0.0.1:3100/openapi.json",
            )

    def test_empty_default_is_honoured(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(expand("${MISSING_VAR:-}"), "")

    def test_split_ref_reports_default(self):
        self.assertEqual(split_ref("A:-b"), ("A", "b"))
        self.assertEqual(split_ref("A"), ("A", None))


class PlainForm(unittest.TestCase):
    def test_unset_var_keeps_the_literal(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(expand("${TERMINAL_API_KEY}"), "${TERMINAL_API_KEY}")

    def test_set_var_uses_value(self):
        with mock.patch.dict("os.environ", {"TERMINAL_API_KEY": "secret-value"}):
            self.assertEqual(expand("${TERMINAL_API_KEY}"), "secret-value")

    def test_empty_value_stays_empty(self):
        # Legacy behaviour: an exported-but-empty var is "", not the literal.
        with mock.patch.dict("os.environ", {"TERMINAL_API_KEY": ""}):
            self.assertEqual(expand("${TERMINAL_API_KEY}"), "")

    def test_mixed_forms_in_one_string(self):
        env = {"GPT_TERMINAL_PLUS_PORT": "4100"}
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertEqual(
                expand("url=${GPT_TERMINAL_PLUS_URL:-http://localhost:3100} key=${TERMINAL_API_KEY}"),
                "url=http://localhost:3100 key=${TERMINAL_API_KEY}",
            )

    def test_non_ref_text_is_untouched(self):
        self.assertEqual(expand("http://localhost:3100/openapi.json"),
                         "http://localhost:3100/openapi.json")


class ManifestAndCli(unittest.TestCase):
    def _manifest(self, tmp: str) -> Path:
        path = Path(tmp) / "servers.json"
        path.write_text(json.dumps({
            "gpt-terminal-plus": {
                "port": 8815,
                "spec_url": "${GPT_TERMINAL_PLUS_URL:-http://localhost:3100}/openapi.json",
                "env": {
                    "API_KEY": "${TERMINAL_API_KEY}",
                    "SERVER_URL_OVERRIDE": "${GPT_TERMINAL_PLUS_URL:-http://localhost:3100}",
                },
            }
        }), encoding="utf-8")
        return path

    def test_expand_json_uses_the_default(self):
        with tempfile.TemporaryDirectory(prefix="mcp-expand-") as tmp:
            path = self._manifest(tmp)
            with mock.patch.dict("os.environ", {}, clear=True):
                cfg = expand_json(path)["gpt-terminal-plus"]
            self.assertEqual(cfg["spec_url"], "http://localhost:3100/openapi.json")
            self.assertEqual(cfg["env"]["SERVER_URL_OVERRIDE"], "http://localhost:3100")

    def test_expand_json_retargets_when_url_is_set(self):
        with tempfile.TemporaryDirectory(prefix="mcp-expand-") as tmp:
            path = self._manifest(tmp)
            with mock.patch.dict("os.environ", {"GPT_TERMINAL_PLUS_URL": "http://localhost:4100"}):
                cfg = expand_json(path)["gpt-terminal-plus"]
            self.assertEqual(cfg["spec_url"], "http://localhost:4100/openapi.json")
            self.assertEqual(cfg["env"]["SERVER_URL_OVERRIDE"], "http://localhost:4100")

    def test_cli_fields_and_env(self):
        with tempfile.TemporaryDirectory(prefix="mcp-expand-") as tmp:
            path = self._manifest(tmp)
            env = {"PATH": "/usr/bin:/bin", "GPT_TERMINAL_PLUS_URL": "http://localhost:4100"}
            out = subprocess.run(
                [sys.executable, str(EXPANDER), "--manifest", str(path),
                 "--server", "gpt-terminal-plus", "--field", "port", "--field", "spec_url"],
                capture_output=True, text=True, env=env, check=True,
            )
            self.assertEqual(out.stdout.split(), ["8815", "http://localhost:4100/openapi.json"])
            out = subprocess.run(
                [sys.executable, str(EXPANDER), "--manifest", str(path),
                 "--server", "gpt-terminal-plus", "--env"],
                capture_output=True, text=True, env=env, check=True,
            )
            self.assertEqual(
                out.stdout.splitlines(),
                ["API_KEY=${TERMINAL_API_KEY}",
                 "SERVER_URL_OVERRIDE=http://localhost:4100"],
            )


class ExampleManifest(unittest.TestCase):
    """The tracked example keeps today's behaviour by default."""

    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((ROOT / "servers.example.json").read_text(encoding="utf-8"))
        cls.gtp = cls.manifest["gpt-terminal-plus"]

    def test_entry_uses_configurable_url_refs(self):
        self.assertEqual(
            self.gtp["spec_url"],
            "${GPT_TERMINAL_PLUS_URL:-http://localhost:3100}/openapi.json",
        )
        self.assertEqual(
            self.gtp["env"]["SERVER_URL_OVERRIDE"],
            "${GPT_TERMINAL_PLUS_URL:-http://localhost:3100}",
        )

    def test_defaults_preserve_localhost_3100(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            cfg = expand(json.dumps(self.gtp))
        self.assertIn("http://localhost:3100/openapi.json", cfg)
        self.assertIn("http://localhost:3100", cfg)


if __name__ == "__main__":
    unittest.main()
