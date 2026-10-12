"""Live inventory facts for mcp-gateway on this host. No secrets."""
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from live_host import is_live_host

ROOT = Path(__file__).resolve().parents[1]
LIVE_HOST = is_live_host(ROOT)
LIVE_HOST_REASON = "requires live gateway host (MCP_LIVE_HOST=1 or servers.json at repo root)"

EXPECTED_SERVER_KEYS = {
    "flyio",
    "render",
    "slack",
    "getzep",
    "virustotal",
    "notion",
    "asana",
    "apisguru",
    "netbox",
    "box",
    "wolframalpha",
    "github",
    "netlify",
    "gpt-terminal-plus",
    "openscad",
    "gh-cli",
    "oci-cli",
    "acpx-cli",
    "wordpress-dev",
}

EXPECTED_RUNNING = {
    "mcp-glama.service",
    "mcp-oauth.service",
    "mcp-status.service",
    "mcp-cli-server@openscad.service",
    "mcp-cli-server@gh-cli.service",
    "mcp-cli-server@oci-cli.service",
    "mcp-cli-server@acpx-cli.service",
}

# wordpress-dev is disabled 2026-09-06 (manifest enabled:false, unit stopped+
# disabled). It must NOT be required running, but the manifest entry (with its
# ${WP_DEV_API_KEY} refs) is kept so it can be re-enabled later.
assert "mcp-server@wordpress-dev.service" not in EXPECTED_RUNNING

# getzep dropped the EXTRA_HEADERS workaround 2026-09-06: it now uses the
# engine's API_AUTH_TYPE=Api-Key scheme-prefix auth (engine >= 0.3.6 behavior).
EXPECTED_DISABLED_KEYS = {"wordpress-dev"}
EXPECTED_GETZEP_ENV = {"API_AUTH_TYPE": "Api-Key", "API_KEY": "${GETZEP_API_KEY}"}

# box :8810 is manual + key-gated. Inventory must not require it listening.
assert "mcp-server@box.service" not in EXPECTED_RUNNING


class LiveInventory(unittest.TestCase):
    @unittest.skipUnless(LIVE_HOST, LIVE_HOST_REASON)
    def test_servers_json_keys_and_ports_only(self):
        path = ROOT / "servers.json"
        self.assertTrue(path.is_file(), "host servers.json (gitignored) is required for live inventory")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(data), EXPECTED_SERVER_KEYS)
        self.assertEqual(data["wordpress-dev"].get("port"), 8817)
        for disabled in EXPECTED_DISABLED_KEYS:
            self.assertFalse(data[disabled].get("enabled", True))
        gz = data["getzep"].get("env") or {}
        self.assertNotIn("EXTRA_HEADERS", gz)
        for k, v in EXPECTED_GETZEP_ENV.items():
            self.assertEqual(gz.get(k), v)
        self.assertEqual(data["openscad"].get("port"), 8816)
        self.assertEqual(data["gh-cli"].get("port"), 8824)
        self.assertEqual(data["box"].get("port"), 8810)
        spec = str(data["wordpress-dev"].get("spec_url") or "")
        self.assertTrue(spec.endswith("wordpress-dev.json") or "wordpress-dev" in spec)
        # Key name only — never assert the token value or that :8810 is listening.
        self.assertIn("BOX_API_KEY", str(data["box"].get("env") or {}))

    def test_example_manifest_has_box_key_ref(self):
        data = json.loads((ROOT / "servers.example.json").read_text(encoding="utf-8"))
        self.assertEqual(data["box"].get("port"), 8810)
        self.assertEqual((data["box"].get("env") or {}).get("API_KEY"), "${BOX_API_KEY}")
        self.assertEqual(data["gpt-terminal-plus"].get("port"), 8815)

    def test_example_manifest_matches_documented_sot_keys(self):
        data = json.loads((ROOT / "servers.example.json").read_text(encoding="utf-8"))
        self.assertEqual(set(data), EXPECTED_SERVER_KEYS)
        self.assertEqual(len(data), 19)
        for parked in ("vibe-cli", "agy-cli", "open-swarm-oracle"):
            self.assertNotIn(parked, data)
        self.assertFalse(data["wordpress-dev"].get("enabled", True))
        self.assertEqual(data["wordpress-dev"].get("port"), 8817)
        wp_env = data["wordpress-dev"].get("env") or {}
        self.assertEqual(wp_env.get("API_KEY"), "${WP_DEV_API_KEY}")
        self.assertEqual(wp_env.get("IGNORE_SSL_TOOLS"), "true")

    @unittest.skipUnless(LIVE_HOST, LIVE_HOST_REASON)
    def test_core_user_units_running(self):
        out = subprocess.check_output(
            [
                "systemctl",
                "--user",
                "list-units",
                "--type=service",
                "--state=running",
                "mcp*",
                "--no-legend",
                "--no-pager",
            ],
            text=True,
        )
        running = {line.split()[0] for line in out.splitlines() if line.strip()}
        missing = EXPECTED_RUNNING - running
        self.assertFalse(missing, missing)

    @unittest.skipUnless(LIVE_HOST, LIVE_HOST_REASON)
    def test_wp_containers_match_docs(self):
        # The Awardspace wordpress-dev bridge is disabled (2026-09-06) and must
        # have no local container. The separate local wp-via-openapi WP stack
        # is wanted (README: /mnt/models/projects/wp-via-openapi) and present.
        out = subprocess.check_output(
            ["docker", "ps", "-a", "--format", "{{.Names}}"], text=True
        ).lower()
        for needle in ("wordpress-dev", "wp-dev"):
            self.assertNotIn(needle, out)
        self.assertIn("wp-via-openapi-wordpress-1", out)

    def test_readme_mentions_wordpress_dev_and_cli_servers(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        for token in ("wordpress-dev", "openscad", "gh-cli", "oci-cli", "acpx-cli", "219.90.166.149"):
            self.assertIn(token, text)
        self.assertIn("wp-via-openapi", text.lower())
        self.assertIn("actionstart-always.local", text)
        self.assertIn("nginx-http-auth", text)
        self.assertIn("**19**", text)
        self.assertIn("mcp-openapi-proxy#69", text)
        self.assertIn("vibe-cli", text)
        self.assertIn("DISABLED-cli.md", text)

    def test_clients_live_cli_table_excludes_parked(self):
        text = (ROOT / "docs/CLIENTS.md").read_text(encoding="utf-8")
        _, _, rest = text.partition("### CLI / agent MCP servers (live)")
        live_block, _, _ = rest.partition("### SaaS")
        self.assertTrue(live_block)
        table, _, after_table = live_block.partition("Parked / disabled")
        for live_cli in ("acpx-cli", "gh-cli", "oci-cli", "openscad"):
            self.assertIn(live_cli, table)
        self.assertNotIn("vibe-cli", table)
        self.assertNotIn("agy-cli", table)
        self.assertIn("vibe-cli", after_table)
        self.assertIn("DISABLED-cli.md", text)
        self.assertIn("wordpress-dev", text)
        self.assertIn("open-swarm-oracle", text)

    def test_unit_descriptions_native_default(self):
        server = (ROOT / "systemd/mcp-server@.service").read_text(encoding="utf-8")
        glama = (ROOT / "systemd/mcp-glama.service").read_text(encoding="utf-8")
        for text in (server, glama):
            self.assertIn("native mcp-openapi-proxy", text)
            self.assertIn("MCP_GATEWAY_HTTP=supergateway", text)
            self.assertNotIn("spec-only supergateway", text)
        self.assertNotIn("supergateway -> uvx", glama)

    def test_status_sh_uses_live_jail_name(self):
        text = (ROOT / "status.sh").read_text(encoding="utf-8")
        self.assertIn("mcp-gateway-auth", text)
        self.assertNotIn("glama-bridge-auth", text)
        self.assertIn("safe-probe", text)

    def test_box_not_required_listening(self):
        self.assertIn("box", EXPECTED_SERVER_KEYS)
        self.assertNotIn("mcp-server@box.service", EXPECTED_RUNNING)
        self.assertTrue(
            all("box" not in u for u in EXPECTED_RUNNING),
            "live inventory must not require mcp-server@box",
        )

    def test_runbook_native_probe_box_rollback(self):
        text = (ROOT / "docs/RUNBOOK.md").read_text(encoding="utf-8")
        for token in (
            "safe-probe",
            "/healthz",
            "BOX_API_KEY",
            "MCP_GATEWAY_HTTP",
            "supergateway",
            "2026-07-28",
            "POST /mcp",
            "mcp-openapi-proxy~=0.3.4",
            "mcp>=1.2.0,<2",
            "short-lived Box token",
        ):
            self.assertIn(token, text)
