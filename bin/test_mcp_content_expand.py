#!/usr/bin/env python3
"""Unit tests for cli_bridge → MCP content expansion."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "mcp_openapi_proxy_cli", ROOT / "bin" / "mcp_openapi_proxy_cli.py"
)
mod = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(mod)


def _bridge_png() -> dict:
    return {
        "exit_code": 0,
        "filename": "cube.png",
        "url": "https://mcp.example.org/sandbox/openscad/cube.png",
        "sandbox_uri": "sandbox://cube.png",
        "output_file": {
            "mime_type": "image/png",
            "size": 12,
            "base64": "aGVsbG8=",
            "filename": "cube.png",
            "data_uri": "data:image/png;base64,aGVsbG8=",
            "url": "https://mcp.example.org/sandbox/openscad/cube.png",
            "sandbox_uri": "sandbox://cube.png",
        },
        "sandbox_file": {"filename": "cube.png", "size": 12},
        "content": [{"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"}],
    }


def _bridge_stl() -> dict:
    return {
        "exit_code": 0,
        "output_file": {
            "mime_type": "model/stl",
            "size": 20,
            "base64": "c29saWQ=",
            "filename": "cube.stl",
        },
        "sandbox_file": {"filename": "cube.stl", "size": 20},
        "content": [{"type": "text", "text": "legacy text"}],
    }


def test_png_blocks_include_image_link_and_markdown():
    from mcp import types

    blocks = mod._content_blocks_from_bridge_response(_bridge_png())
    types = [type(b).__name__ for b in blocks]
    assert types[0] == "ImageContent"
    assert types[1] == "ResourceLink"
    assert types[2] == "TextContent"
    assert blocks[0].data == "aGVsbG8="
    assert str(blocks[1].uri) == "https://mcp.example.org/sandbox/openscad/cube.png"
    assert "![OpenSCAD preview](data:image/png;base64," in blocks[2].text
    assert "https://mcp.example.org/sandbox/openscad/cube.png" in blocks[2].text


def test_stl_blocks_include_embedded_and_link():
    from mcp import types

    blocks = mod._content_blocks_from_bridge_response(_bridge_stl())
    assert any(isinstance(b, types.EmbeddedResource) for b in blocks)
    assert any(isinstance(b, types.ResourceLink) for b in blocks)
    assert any(isinstance(b, types.TextContent) and "cube.stl" in b.text for b in blocks)


def test_structured_content_has_data_uri_and_url():
    structured = mod._structured_content_from_bridge(_bridge_png())
    assert structured["output_file"]["data_uri"].startswith("data:image/png;base64,")
    assert structured["output_file"]["url"].endswith("/cube.png")
    assert structured["url"].endswith("/cube.png")
    assert structured["sandbox_uri"] == "sandbox://cube.png"
    stl_structured = mod._structured_content_from_bridge(_bridge_stl())
    assert "base64" not in stl_structured["output_file"]
    assert "data_uri" not in stl_structured["output_file"]


def test_filename_only_blocks():
    from mcp import types

    data = {
        "exit_code": 0,
        "filename": "cube.png",
        "sandbox_uri": "sandbox://cube.png",
        "output_file": {
            "filename": "cube.png",
            "mime_type": "image/png",
            "size": 12,
            "sandbox_uri": "sandbox://cube.png",
        },
        "sandbox_file": {"filename": "cube.png", "size": 12},
    }
    blocks = mod._content_blocks_from_bridge_response(data)
    assert len(blocks) == 2
    assert isinstance(blocks[0], types.ResourceLink)
    assert isinstance(blocks[1], types.TextContent)
    assert "resources/read" in blocks[1].text


def test_filename_only_blocks_with_public_url():
    from mcp import types

    url = "https://mcp.example.org/sandbox/openscad/previews/cube.png"
    data = {
        "exit_code": 0,
        "filename": "previews/cube.png",
        "url": url,
        "sandbox_uri": "sandbox://previews/cube.png",
        "output_file": {
            "filename": "previews/cube.png",
            "mime_type": "image/png",
            "size": 12,
            "url": url,
            "sandbox_uri": "sandbox://previews/cube.png",
        },
        "sandbox_file": {"filename": "previews/cube.png", "size": 12},
    }
    blocks = mod._content_blocks_from_bridge_response(data)
    assert len(blocks) == 2
    assert isinstance(blocks[0], types.ResourceLink)
    assert str(blocks[0].uri) == url
    assert isinstance(blocks[1], types.TextContent)
    assert f"![OpenSCAD preview]({url})" in blocks[1].text
    structured = mod._structured_content_from_bridge(data)
    assert structured["url"] == url


if __name__ == "__main__":
    test_png_blocks_include_image_link_and_markdown()
    test_stl_blocks_include_embedded_and_link()
    test_structured_content_has_data_uri_and_url()
    test_filename_only_blocks()
    test_filename_only_blocks_with_public_url()
    print("ok")