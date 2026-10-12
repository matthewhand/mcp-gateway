"""Live-host gating for the mcp-gateway test suite.

Several tests are "live inventory" audits that assert production state on the
gateway host: user systemd units, the gitignored `servers.json` manifest, and
the oracle-era `~/.config/swarm` layout. On a dev/CI box those state files do
not exist, so those tests must skip instead of fail.

A host counts as the live gateway when:

* `MCP_LIVE_HOST=1` is set (explicit; use on the production host), or
* `servers.json` (the production manifest, gitignored) exists at the repo root.

Set `MCP_LIVE_HOST=0` to force non-live even when a manifest is present
(e.g. CI checking out a copy that happens to carry one).
"""
from __future__ import annotations

import os
from pathlib import Path


def is_live_host(root: Path | str) -> bool:
    """True when the current host is the production mcp-gateway host."""
    root = Path(root)
    flag = os.environ.get("MCP_LIVE_HOST")
    if flag == "1":
        return True
    if flag == "0":
        return False
    # Auto-detect: the production manifest exists only on the live gateway host.
    return (root / "servers.json").is_file()