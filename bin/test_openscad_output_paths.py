#!/usr/bin/env python3
"""TDD: OpenSCAD output paths — image+link (default) vs URL-only.

Proves the two client-facing modes:

1. **Default** (``return_inline=true`` / omitted for PNG):
   bridge embeds base64 AND sandbox url/uri; proxy emits ImageContent +
   ResourceLink + text. File is always saved when ``persist_output``.

2. **URL-only** (``return_inline=false``):
   bridge returns metadata + public URL, no base64; proxy emits ResourceLink
   + markdown image pointing at HTTPS; static ``GET /sandbox/…`` serves raw
   bytes without auth.

3. **STL** defaults to URL-only (``return_inline_default: false`` on the op).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    # dataclasses need the module registered before class body runs
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


bridge = _load("cli_bridge", ROOT / "bin" / "cli_bridge.py")
proxy = _load("mcp_openapi_proxy_cli", ROOT / "bin" / "mcp_openapi_proxy_cli.py")

PUBLIC_BASE = "https://mcp.example.org"
PUBLIC_URL = f"{PUBLIC_BASE}/sandbox/openscad/cube.png"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-payload"
PNG_B64 = __import__("base64").b64encode(PNG_BYTES).decode("ascii")

RENDER_PNG_OP = {"id": "render_png", "return_inline_default": True, "persist_output": True}
RENDER_STL_OP = {"id": "render_stl", "return_inline_default": False, "persist_output": True}


def _openscad_cfg(sandbox: Path | None = None) -> Any:
    cfg = bridge.BridgeConfig.load(ROOT / "configs" / "openscad.json")
    if sandbox is not None:
        cfg.working_directory = {
            "mode": "fixed",
            "path": str(sandbox),
            "mkdir": True,
        }
    return cfg


def _bridge_result_after_render(
    cfg: Any,
    *,
    body: dict[str, Any],
    candidate: Path,
    persist_output: bool = True,
    op: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mirror the post-process branch in ``execute_operation`` (no OpenSCAD binary)."""
    data = candidate.read_bytes()
    run_cwd = str(candidate.parent)
    op = op or RENDER_PNG_OP
    result: dict[str, Any] = {
        "stdout": "",
        "stderr": "",
        "exit_code": 0,
        "argv": ["openscad", "-o", str(candidate)],
    }
    inline = bridge._return_inline(body, cfg, op)
    rel = bridge._relative_sandbox_name(cfg, candidate, run_cwd)
    if inline:
        output_file = bridge._build_output_file(data, str(candidate), cfg, relpath=rel)
        result["output_file"] = output_file
        result["filename"] = rel
        result["sandbox_uri"] = output_file["sandbox_uri"]
        if output_file.get("url"):
            result["url"] = output_file["url"]
        if persist_output:
            result["sandbox_file"] = {
                "filename": rel,
                "path": str(candidate),
                "size": output_file["size"],
            }
        if cfg.inline_binary_outputs:
            result["content"] = bridge._mcp_content_blocks(
                output_file["mime_type"],
                output_file["base64"],
                output_file["size"],
                public_url=output_file.get("url"),
                filename=rel,
            )
    else:
        meta = bridge._build_output_metadata(cfg, data, str(candidate), relpath=rel)
        result["output_file"] = meta
        result["filename"] = meta["filename"]
        result["sandbox_uri"] = meta["sandbox_uri"]
        if meta.get("url"):
            result["url"] = meta["url"]
        if persist_output:
            result["sandbox_file"] = {
                "filename": rel,
                "path": str(candidate),
                "size": meta["size"],
            }
        result["content"] = [{"type": "text", "text": bridge._saved_file_message(meta)}]
    return result


# ---------------------------------------------------------------------------
# return_inline selection (openscad config defaults)
# ---------------------------------------------------------------------------


def test_return_inline_defaults_true_for_png():
    cfg = _openscad_cfg()
    assert bridge._return_inline({}, cfg, RENDER_PNG_OP) is True
    assert bridge._return_inline(None, cfg, RENDER_PNG_OP) is True
    assert bridge._return_inline({"return_inline": True}, cfg, RENDER_PNG_OP) is True


def test_return_inline_false_when_requested():
    cfg = _openscad_cfg()
    assert bridge._return_inline({"return_inline": False}, cfg, RENDER_PNG_OP) is False


def test_return_inline_stl_defaults_false():
    cfg = _openscad_cfg()
    assert bridge._return_inline({}, cfg, RENDER_STL_OP) is False
    assert bridge._return_inline({"return_inline": True}, cfg, RENDER_STL_OP) is True


def test_config_body_param_default_is_true():
    cfg = _openscad_cfg()
    assert cfg.body_params["return_inline"]["default"] is True


# ---------------------------------------------------------------------------
# Path 1: URL-only (return_inline=false)
# ---------------------------------------------------------------------------


def test_url_path_bridge_has_public_url_no_base64():
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        png = sandbox / "cube.png"
        png.write_bytes(PNG_BYTES)
        cfg = _openscad_cfg(sandbox)

        with mock.patch.dict(os.environ, {"MCP_PUBLIC_URL": PUBLIC_BASE}, clear=False):
            result = _bridge_result_after_render(
                cfg, body={"return_inline": False}, candidate=png
            )

        of = result["output_file"]
        assert "base64" not in of
        assert "data_uri" not in of
        assert of["filename"] == "cube.png"
        assert of["url"] == PUBLIC_URL
        assert result["url"] == PUBLIC_URL
        assert result["sandbox_uri"] == "sandbox://cube.png"
        assert "base64" not in json.dumps(result)


def test_url_path_without_mcp_public_url_omits_url():
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        png = sandbox / "cube.png"
        png.write_bytes(PNG_BYTES)
        cfg = _openscad_cfg(sandbox)

        env = {k: v for k, v in os.environ.items() if k not in ("MCP_PUBLIC_URL",)}
        with mock.patch.dict(os.environ, env, clear=True):
            os.environ.pop("MCP_PUBLIC_URL", None)
            result = _bridge_result_after_render(
                cfg, body={"return_inline": False}, candidate=png
            )

        assert "url" not in result
        assert "url" not in result["output_file"]
        assert result["sandbox_uri"] == "sandbox://cube.png"
        assert "base64" not in result["output_file"]


def test_url_path_proxy_emits_resourcelink_and_markdown_not_imagecontent():
    from mcp import types

    bridge_json = {
        "exit_code": 0,
        "filename": "cube.png",
        "url": PUBLIC_URL,
        "sandbox_uri": "sandbox://cube.png",
        "output_file": {
            "filename": "cube.png",
            "mime_type": "image/png",
            "size": len(PNG_BYTES),
            "url": PUBLIC_URL,
            "sandbox_uri": "sandbox://cube.png",
        },
        "sandbox_file": {"filename": "cube.png", "size": len(PNG_BYTES)},
        "content": [
            {
                "type": "text",
                "text": f"Saved `cube.png`.\n\n![OpenSCAD preview]({PUBLIC_URL})\n\nPreview URL: {PUBLIC_URL}",
            }
        ],
    }
    blocks = proxy._content_blocks_from_bridge_response(bridge_json)
    assert not any(isinstance(b, types.ImageContent) for b in blocks)
    assert any(isinstance(b, types.ResourceLink) for b in blocks)
    link = next(b for b in blocks if isinstance(b, types.ResourceLink))
    assert str(link.uri) == PUBLIC_URL
    text = next(b for b in blocks if isinstance(b, types.TextContent))
    assert f"![OpenSCAD preview]({PUBLIC_URL})" in text.text
    assert PNG_B64 not in text.text

    structured = proxy._structured_content_from_bridge(bridge_json)
    assert structured["url"] == PUBLIC_URL
    assert "base64" not in structured.get("output_file", {})


def test_static_sandbox_endpoint_serves_bytes_without_auth():
    """Public hosting path: GET /sandbox/<file> returns raw PNG (no auth layer)."""
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        png = sandbox / "cube.png"
        png.write_bytes(PNG_BYTES)
        cfg = _openscad_cfg(sandbox)

        code, body, ctype = bridge._serve_sandbox_static(cfg, "/sandbox/cube.png")
        assert code == 200
        assert body == PNG_BYTES
        assert ctype == "image/png"

        code404, body404, ctype404 = bridge._serve_sandbox_static(cfg, "/sandbox/missing.png")
        assert code404 == 404
        assert ctype404 == "application/json"


def test_public_url_builder_uses_slug():
    cfg = _openscad_cfg()
    with mock.patch.dict(os.environ, {"MCP_PUBLIC_URL": PUBLIC_BASE}, clear=False):
        assert bridge._sandbox_public_url(cfg, "cube.png") == PUBLIC_URL
        assert (
            bridge._sandbox_public_url(cfg, "previews/job1.png")
            == f"{PUBLIC_BASE}/sandbox/openscad/previews/job1.png"
        )


# ---------------------------------------------------------------------------
# Path 2: default inline base64 + resource link
# ---------------------------------------------------------------------------


def test_default_path_bridge_embeds_base64_and_sandbox_url():
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        png = sandbox / "cube.png"
        png.write_bytes(PNG_BYTES)
        cfg = _openscad_cfg(sandbox)

        with mock.patch.dict(os.environ, {"MCP_PUBLIC_URL": PUBLIC_BASE}, clear=False):
            # empty body → default true for PNG
            result = _bridge_result_after_render(cfg, body={}, candidate=png)

        of = result["output_file"]
        assert of["base64"] == PNG_B64
        assert of["data_uri"] == f"data:image/png;base64,{PNG_B64}"
        assert of["mime_type"] == "image/png"
        assert of["url"] == PUBLIC_URL
        assert of["sandbox_uri"] == "sandbox://cube.png"
        assert result["url"] == PUBLIC_URL
        assert result["sandbox_uri"] == "sandbox://cube.png"
        assert result["sandbox_file"]["filename"] == "cube.png"
        assert result["content"][0]["type"] == "image"
        assert result["content"][0]["data"] == PNG_B64


def test_default_path_proxy_emits_imagecontent_and_resourcelink():
    from mcp import types

    bridge_json = {
        "exit_code": 0,
        "filename": "cube.png",
        "url": PUBLIC_URL,
        "sandbox_uri": "sandbox://cube.png",
        "output_file": {
            "mime_type": "image/png",
            "size": len(PNG_BYTES),
            "base64": PNG_B64,
            "filename": "cube.png",
            "data_uri": f"data:image/png;base64,{PNG_B64}",
            "url": PUBLIC_URL,
            "sandbox_uri": "sandbox://cube.png",
        },
        "sandbox_file": {"filename": "cube.png", "size": len(PNG_BYTES)},
        "content": [{"type": "image", "data": PNG_B64, "mimeType": "image/png"}],
    }
    blocks = proxy._content_blocks_from_bridge_response(bridge_json)
    assert isinstance(blocks[0], types.ImageContent)
    assert blocks[0].data == PNG_B64
    assert blocks[0].mimeType == "image/png"
    assert any(isinstance(b, types.ResourceLink) for b in blocks)
    link = next(b for b in blocks if isinstance(b, types.ResourceLink))
    assert str(link.uri) == PUBLIC_URL
    assert link.mimeType == "image/png"
    assert any(
        isinstance(b, types.TextContent) and "data:image/png;base64," in b.text
        for b in blocks
    )
    assert any(
        isinstance(b, types.TextContent) and PUBLIC_URL in b.text for b in blocks
    )

    structured = proxy._structured_content_from_bridge(bridge_json)
    assert structured["url"] == PUBLIC_URL
    assert structured["output_file"]["url"] == PUBLIC_URL
    assert structured["output_file"]["data_uri"].startswith("data:image/png;base64,")
    assert "base64" not in structured["output_file"]


def test_inline_without_sandbox_metadata_skips_resource_link():
    """download_file-style payload: image only, no ResourceLink required."""
    from mcp import types

    bridge_json = {
        "exit_code": 0,
        "output_file": {
            "mime_type": "image/png",
            "size": len(PNG_BYTES),
            "base64": PNG_B64,
            "filename": "cube.png",
            "data_uri": f"data:image/png;base64,{PNG_B64}",
        },
        "content": [{"type": "image", "data": PNG_B64, "mimeType": "image/png"}],
    }
    blocks = proxy._content_blocks_from_bridge_response(bridge_json)
    assert isinstance(blocks[0], types.ImageContent)
    assert not any(isinstance(b, types.ResourceLink) for b in blocks)


# ---------------------------------------------------------------------------
# End-to-end: same sandbox file, both modes differ only by return_inline
# ---------------------------------------------------------------------------


def test_both_paths_end_to_end_same_file():
    from mcp import types

    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        png = sandbox / "cube.png"
        png.write_bytes(PNG_BYTES)
        cfg = _openscad_cfg(sandbox)

        with mock.patch.dict(os.environ, {"MCP_PUBLIC_URL": PUBLIC_BASE}, clear=False):
            default_result = _bridge_result_after_render(cfg, body={}, candidate=png)
            url_result = _bridge_result_after_render(
                cfg, body={"return_inline": False}, candidate=png
            )

        # Bridge contract
        assert "base64" not in url_result["output_file"]
        assert url_result["url"] == PUBLIC_URL
        assert default_result["output_file"]["base64"] == PNG_B64
        assert default_result["url"] == PUBLIC_URL

        # Proxy contract
        url_blocks = proxy._content_blocks_from_bridge_response(url_result)
        default_blocks = proxy._content_blocks_from_bridge_response(default_result)

        assert any(isinstance(b, types.ResourceLink) for b in url_blocks)
        assert not any(isinstance(b, types.ImageContent) for b in url_blocks)

        assert isinstance(default_blocks[0], types.ImageContent)
        assert default_blocks[0].data == PNG_B64
        assert any(isinstance(b, types.ResourceLink) for b in default_blocks)
        assert (
            str(next(b for b in default_blocks if isinstance(b, types.ResourceLink)).uri)
            == PUBLIC_URL
        )

        # Static host still serves the file either way (persist_output)
        code, body, _ = bridge._serve_sandbox_static(cfg, "/sandbox/cube.png")
        assert code == 200 and body == PNG_BYTES


def main() -> None:
    tests = [
        test_return_inline_defaults_true_for_png,
        test_return_inline_false_when_requested,
        test_return_inline_stl_defaults_false,
        test_config_body_param_default_is_true,
        test_url_path_bridge_has_public_url_no_base64,
        test_url_path_without_mcp_public_url_omits_url,
        test_url_path_proxy_emits_resourcelink_and_markdown_not_imagecontent,
        test_static_sandbox_endpoint_serves_bytes_without_auth,
        test_public_url_builder_uses_slug,
        test_default_path_bridge_embeds_base64_and_sandbox_url,
        test_default_path_proxy_emits_imagecontent_and_resourcelink,
        test_inline_without_sandbox_metadata_skips_resource_link,
        test_both_paths_end_to_end_same_file,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL  {t.__name__}: {exc}")
            raise
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    if failed:
        sys.exit(1)
    print("ok")


if __name__ == "__main__":
    main()
