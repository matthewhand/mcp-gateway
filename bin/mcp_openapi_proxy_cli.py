#!/usr/bin/env python3
"""MCP OpenAPI proxy entry for mcp-gateway.

Naming improvements over stock mcp-openapi-proxy:

1. Strip uninformative path segments (``/api/``, ``/v1/``, …) before name generation.
2. Strip ``post_v1_`` / ``get_v1_`` / … prefixes from fallback names (safer than
   bare ``post_`` / ``get_``, which false-positive on resources like ``post_posts``).
3. Optional ``TOOL_NAME_USE_OPERATION_ID=true`` (CLI bridges): use OpenAPI
   ``operationId`` directly (``render``, ``prompt``, …).
4. Expand CLI bridge image responses into MCP ``ImageContent`` blocks (stock
   mcp-openapi-proxy stringifies all JSON as ``TextContent``, which clients
   cannot render as images).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

_HTTP = frozenset({"get", "post", "put", "delete", "patch", "options", "head", "trace"})
_OP_ID_MAP: dict[tuple[str, str], str] = {}
_PATH_PREFIX_RE = re.compile(r"/(api|rest|public)/?", re.IGNORECASE)
_VERSION_SEGMENT_RE = re.compile(r"/v\d+/?", re.IGNORECASE)
_METHOD_VERSION_PREFIX_RE = re.compile(
    r"^(get|post|put|delete|patch|options|head)_v\d+_",
    re.IGNORECASE,
)


def _truthy(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.lower() in ("1", "true", "yes")


def _sanitize_tool_name(name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name or "unknown_tool"


def _apply_name_limits(tool_name: str, max_length: Optional[int]) -> str:
    from mcp_openapi_proxy.utils import get_tool_name_max_length

    prefix = os.getenv("TOOL_NAME_PREFIX", "")
    if prefix:
        tool_name = f"{prefix}{tool_name}"

    effective_max_length = max_length if max_length is not None else get_tool_name_max_length()
    final_limit = 64
    if effective_max_length is not None and effective_max_length < final_limit:
        final_limit = effective_max_length
    if len(tool_name) > final_limit:
        tool_name = tool_name[:final_limit]
    return tool_name


def _strip_uninformative_path_segments(path: str) -> str:
    """Drop noisy URL prefixes before tool-name generation."""
    if not _truthy("TOOL_NAME_STRIP_PATH_PREFIXES", default=True):
        return path
    path = _PATH_PREFIX_RE.sub("/", path)
    while True:
        new_path = _VERSION_SEGMENT_RE.sub("/", path, count=1)
        if new_path == path:
            break
        path = new_path
    path = re.sub(r"/+", "/", path)
    return path.lstrip("/").rstrip("/")


def _strip_method_version_prefix(name: str) -> str:
    """Remove post_v1_/get_v1_/… when a version segment still appears in the name."""
    if not _truthy("TOOL_NAME_STRIP_METHOD_VERSION_PREFIX", default=True):
        return name
    match = _METHOD_VERSION_PREFIX_RE.match(name)
    if match:
        return name[match.end() :]
    return name


def _normalize_raw_name(raw_name: str) -> str:
    if " " not in raw_name:
        return raw_name
    method, path = raw_name.split(" ", 1)
    return f"{method} {_strip_uninformative_path_segments(path)}"


def _load_operation_id_map() -> None:
    from mcp_openapi_proxy.utils import fetch_openapi_spec

    url = os.getenv("OPENAPI_SPEC_URL")
    if not url:
        return
    spec = fetch_openapi_spec(url)
    if not spec:
        return
    for path, path_item in spec.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in _HTTP or not isinstance(operation, dict):
                continue
            oid = operation.get("operationId")
            if oid:
                _OP_ID_MAP[(method.upper(), path)] = str(oid)


def _patch_normalize_tool_name() -> None:
    import mcp_openapi_proxy.utils as utils
    from mcp_openapi_proxy.logging_setup import logger

    original = utils.normalize_tool_name

    def normalize_tool_name(raw_name: str, max_length: Optional[int] = None) -> str:
        if _truthy("TOOL_NAME_USE_OPERATION_ID") and " " in raw_name:
            method, path = raw_name.split(" ", 1)
            oid = _OP_ID_MAP.get((method.upper(), path))
            if oid:
                name = _apply_name_limits(_sanitize_tool_name(oid), max_length)
                logger.info(f"Final tool name: {name}, length: {len(name)}")
                return name

        normalized_raw = _normalize_raw_name(raw_name)
        name = _strip_method_version_prefix(original(normalized_raw, max_length))
        return name

    utils.normalize_tool_name = normalize_tool_name


def _image_mime_type(item: dict[str, Any]) -> str:
    return str(item.get("mimeType") or item.get("mime_type") or "image/png")


def _sandbox_resource_uri(data: dict[str, Any], output_file: dict[str, Any]) -> str:
    sandbox = data.get("sandbox_file") if isinstance(data.get("sandbox_file"), dict) else {}
    filename = sandbox.get("filename") or output_file.get("filename") or "output"
    return f"sandbox://{filename}"


def _output_data_uri(output_file: dict[str, Any]) -> str | None:
    if output_file.get("data_uri"):
        return str(output_file["data_uri"])
    mime = output_file.get("mime_type")
    b64 = output_file.get("base64")
    if mime and b64:
        return f"data:{mime};base64,{b64}"
    return None


def _public_file_url(data: dict[str, Any], output_file: dict[str, Any]) -> str | None:
    for source in (output_file, data):
        if isinstance(source, dict) and source.get("url"):
            return str(source["url"])
    return None


def _filename_only_blocks(data: dict[str, Any], output_file: dict[str, Any]) -> list[Any]:
    from mcp import types

    uri = str(output_file.get("sandbox_uri") or _sandbox_resource_uri(data, output_file))
    filename = str(output_file.get("filename") or "output")
    mime = str(output_file.get("mime_type", "application/octet-stream"))
    size = int(output_file.get("size") or 0)
    url = _public_file_url(data, output_file)
    if url and mime.startswith("image/"):
        text = (
            f"Saved `{filename}` ({mime}, {size} bytes).\n\n"
            f"![OpenSCAD preview]({url})\n\nPreview URL: {url}"
        )
    elif url:
        text = f"Saved `{filename}` ({mime}, {size} bytes).\n\nDownload URL: {url}"
    else:
        text = (
            f"Saved `{filename}` ({mime}, {size} bytes). "
            f"Fetch bytes via resources/read `{uri}` or call download_file."
        )

    blocks: list[Any] = [
        types.TextContent(type="text", text=text),
    ]
    if url:
        blocks.insert(
            0,
            types.ResourceLink(
                type="resource_link",
                uri=url,
                name=filename,
                mime_type=mime,
                size=size or None,
                description=f"OpenSCAD sandbox artifact {filename}",
            ),
        )
    else:
        blocks.insert(
            0,
            types.ResourceLink(
                type="resource_link",
                uri=uri,
                name=filename,
                mime_type=mime,
                size=size or None,
                description=f"OpenSCAD sandbox artifact {filename}",
            ),
        )
    return blocks


def _structured_content_from_bridge(data: dict[str, Any]) -> dict[str, Any] | None:
    """Machine-readable payload for clients that read structuredContent instead of content blocks."""
    if data.get("exit_code") not in (None, 0):
        return None
    structured: dict[str, Any] = {}
    if data.get("filename"):
        structured["filename"] = data["filename"]
    if data.get("sandbox_uri"):
        structured["sandbox_uri"] = data["sandbox_uri"]
    if data.get("url"):
        structured["url"] = data["url"]
    output_file = data.get("output_file")
    if isinstance(output_file, dict) and output_file.get("base64"):
        mime = str(output_file.get("mime_type", "application/octet-stream"))
        entry = {
            k: output_file[k]
            for k in ("filename", "mime_type", "size")
            if k in output_file
        }
        entry["sandbox_uri"] = _sandbox_resource_uri(data, output_file)
        url = _public_file_url(data, output_file)
        if url:
            entry["url"] = url
            structured.setdefault("url", url)
        if mime.startswith("image/"):
            data_uri = _output_data_uri(output_file)
            if data_uri:
                entry["data_uri"] = data_uri
        structured["output_file"] = entry
    elif isinstance(output_file, dict) and output_file.get("filename"):
        structured["output_file"] = {
            k: output_file[k]
            for k in ("filename", "mime_type", "size", "sandbox_uri", "url")
            if k in output_file
        }
        url = _public_file_url(data, output_file)
        if url:
            structured.setdefault("url", url)
    sandbox = data.get("sandbox_file")
    if isinstance(sandbox, dict) and sandbox.get("filename"):
        structured["sandbox_file"] = dict(sandbox)
        structured.setdefault("sandbox_uri", f"sandbox://{sandbox['filename']}")
    return structured or None


def _embedded_blob_block(data: dict[str, Any], output_file: dict[str, Any]) -> Any:
    from mcp import types

    mime = str(output_file.get("mime_type", "application/octet-stream"))
    uri = _sandbox_resource_uri(data, output_file)
    return types.EmbeddedResource(
        type="resource",
        resource=types.BlobResourceContents(
            uri=uri,
            mime_type=mime,
            blob=str(output_file["base64"]),
        ),
    )


def _resource_link_block(data: dict[str, Any], output_file: dict[str, Any]) -> Any:
    from mcp import types

    mime = str(output_file.get("mime_type", "application/octet-stream"))
    filename = str(output_file.get("filename") or "output")
    # Prefer public HTTPS URL when available so clients can fetch without MCP session.
    public = _public_file_url(data, output_file)
    uri = public or _sandbox_resource_uri(data, output_file)
    return types.ResourceLink(
        type="resource_link",
        uri=uri,
        name=filename,
        mime_type=mime,
        size=int(output_file.get("size") or 0) or None,
        description=f"OpenSCAD sandbox artifact {filename}",
    )


def _has_sandbox_link(data: dict[str, Any], output_file: dict[str, Any]) -> bool:
    if _public_file_url(data, output_file):
        return True
    if data.get("sandbox_uri") or output_file.get("sandbox_uri"):
        return True
    sandbox = data.get("sandbox_file")
    return isinstance(sandbox, dict) and bool(sandbox.get("filename"))


def _image_fallback_text(data: dict[str, Any], output_file: dict[str, Any]) -> str:
    mime = str(output_file.get("mime_type", "image/png"))
    size = int(output_file.get("size") or 0)
    filename = str(output_file.get("filename") or "output")
    data_uri = _output_data_uri(output_file)
    public = _public_file_url(data, output_file)
    lines: list[str] = []
    if data_uri:
        lines.append(f"![OpenSCAD preview]({data_uri})")
        lines.append("")
    lines.append(f"PNG preview `{filename}` ({mime}, {size} bytes).")
    if public:
        lines.append("")
        lines.append(f"Also saved — Preview URL: {public}")
    return "\n".join(lines)


def _binary_fallback_text(data: dict[str, Any], output_file: dict[str, Any]) -> str:
    mime = str(output_file.get("mime_type", "application/octet-stream"))
    size = int(output_file.get("size") or 0)
    filename = str(output_file.get("filename") or "output")
    uri = _sandbox_resource_uri(data, output_file)
    return (
        f"Sandbox file `{filename}` ({mime}, {size} bytes) at `{uri}`. "
        "Binary attached as MCP resource blob; use `download_file` to fetch again."
    )


def _content_blocks_from_bridge_response(data: dict[str, Any]) -> list[Any] | None:
    """Build MCP content blocks from cli_bridge JSON (ImageContent, blobs, TextContent)."""
    from mcp import types

    if data.get("exit_code") not in (None, 0):
        return None

    blocks: list[Any] = []
    output_file = data.get("output_file") if isinstance(data.get("output_file"), dict) else None

    if output_file and not output_file.get("base64") and (
        data.get("sandbox_uri") or output_file.get("sandbox_uri") or data.get("sandbox_file")
    ):
        return _filename_only_blocks(data, output_file)

    if output_file and output_file.get("base64"):
        mime = str(output_file.get("mime_type", "application/octet-stream"))
        if mime.startswith("image/"):
            # Default image path: ImageContent (base64) + ResourceLink (saved file) + text.
            blocks.append(
                types.ImageContent(
                    type="image",
                    data=str(output_file["base64"]),
                    mime_type=mime,
                )
            )
            if _has_sandbox_link(data, output_file):
                blocks.append(_resource_link_block(data, output_file))
            blocks.append(
                types.TextContent(type="text", text=_image_fallback_text(data, output_file))
            )
        else:
            blocks.append(_embedded_blob_block(data, output_file))
            blocks.append(_resource_link_block(data, output_file))
            blocks.append(types.TextContent(type="text", text=_binary_fallback_text(data, output_file)))
        return blocks

    for item in data.get("content") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "image" and item.get("data"):
            blocks.append(
                types.ImageContent(
                    type="image",
                    data=str(item["data"]),
                    mime_type=_image_mime_type(item),
                )
            )
        elif item.get("type") == "text" and item.get("text"):
            blocks.append(types.TextContent(type="text", text=str(item["text"])))

    return blocks or None


def _expand_call_tool_result(result: Any) -> Any:
    """Upgrade stringified JSON tool results to rich MCP content blocks."""
    from mcp import types

    if not _truthy("TOOL_RESULT_EXPAND_IMAGES", default=True):
        return result
    if not getattr(result, "content", None) or len(result.content) != 1:
        return result

    block = result.content[0]
    if not isinstance(block, types.TextContent):
        return result

    try:
        data = json.loads(block.text)
    except json.JSONDecodeError:
        return result
    if not isinstance(data, dict):
        return result

    expanded = _content_blocks_from_bridge_response(data)
    if not expanded:
        return result

    return types.CallToolResult(
        content=expanded,
        structured_content=_structured_content_from_bridge(data),
        is_error=getattr(result, "is_error", None) or getattr(result, "isError", False),
    )


def _cli_bridge_base_url() -> str:
    return (os.getenv("SERVER_URL_OVERRIDE") or "http://127.0.0.1:3860").rstrip("/")


def _fetch_sandbox_file(filepath: str) -> tuple[bytes, str]:
    import base64
    import urllib.parse
    import urllib.request

    relpath = filepath.lstrip("/")
    url = f"{_cli_bridge_base_url()}/files/{urllib.parse.quote(relpath, safe='')}"
    with urllib.request.urlopen(url, timeout=120) as resp:
        payload = json.loads(resp.read().decode())
    output_file = payload.get("output_file") if isinstance(payload.get("output_file"), dict) else None
    if not output_file or not output_file.get("base64"):
        raise ValueError(f"sandbox file missing output_file payload: {relpath}")
    return base64.b64decode(str(output_file["base64"])), str(
        output_file.get("mime_type") or "application/octet-stream"
    )


def _patch_sandbox_resources() -> None:
    from mcp import types
    import mcp_openapi_proxy.server_lowlevel as server_lowlevel
    from mcp_openapi_proxy.logging_setup import logger
    from mcp_openapi_proxy.protocol import unpack_handler_args

    original_list = server_lowlevel.list_resources
    original_read = server_lowlevel.read_resource

    async def list_resources(ctx_or_request: Any, params: Any = None) -> Any:
        result = await original_list(ctx_or_request, params)
        try:
            import urllib.request

            with urllib.request.urlopen(f"{_cli_bridge_base_url()}/files", timeout=30) as resp:
                payload = json.loads(resp.read().decode())
            files = payload if isinstance(payload, list) else payload.get("files", [])
            extra = []
            for item in files:
                if not isinstance(item, dict) or not item.get("filename"):
                    continue
                name = str(item["filename"])
                mime = {
                    ".png": "image/png",
                    ".stl": "model/stl",
                    ".scad": "application/x-openscad",
                }.get(Path(name).suffix.lower(), "application/octet-stream")
                extra.append(
                    types.Resource(
                        uri=f"sandbox://{name}",
                        name=name,
                        description=f"OpenSCAD sandbox file {name}",
                        mime_type=mime,
                        size=int(item.get("size") or 0) or None,
                    )
                )
            if extra:
                merged = list(getattr(result, "resources", []) or []) + extra
                result = types.ListResourcesResult(resources=merged)
        except Exception as exc:
            logger.debug(f"sandbox list_resources skipped: {exc}")
        return result

    async def read_resource(ctx_or_request: Any, params: Any = None) -> Any:
        _, params = unpack_handler_args(ctx_or_request, params)
        uri = str(params.uri)
        if uri.startswith("sandbox://"):
            filename = uri[len("sandbox://") :]
            try:
                data, mime = _fetch_sandbox_file(filename)
                import base64

                if mime.startswith("image/") or mime.endswith("stl") or mime == "model/stl":
                    return types.ReadResourceResult(
                        contents=[
                            types.BlobResourceContents(
                                uri=uri,
                                mime_type=mime,
                                blob=base64.b64encode(data).decode("ascii"),
                            )
                        ]
                    )
                return types.ReadResourceResult(
                    contents=[
                        types.TextResourceContents(
                            uri=uri,
                            mime_type=mime,
                            text=data.decode("utf-8", errors="replace"),
                        )
                    ]
                )
            except Exception as exc:
                logger.error(f"sandbox read_resource failed for {uri}: {exc}")
                return types.ReadResourceResult(
                    contents=[
                        types.TextResourceContents(
                            uri=uri,
                            text=f"Failed to read sandbox resource: {exc}",
                        )
                    ]
                )
        return await original_read(ctx_or_request, params)

    server_lowlevel.list_resources = list_resources
    server_lowlevel.read_resource = read_resource


def _patch_call_tool_image_responses() -> None:
    import mcp_openapi_proxy.server_lowlevel as server_lowlevel
    from mcp_openapi_proxy.logging_setup import logger

    original = server_lowlevel.dispatcher_handler

    async def dispatcher_handler(ctx_or_request: Any, params: Any = None) -> Any:
        result = await original(ctx_or_request, params)
        expanded = _expand_call_tool_result(result)
        if expanded is not result:
            logger.debug("Expanded tool result with MCP ImageContent block(s)")
        return expanded

    server_lowlevel.dispatcher_handler = dispatcher_handler


def main() -> None:
    _load_operation_id_map()
    _patch_normalize_tool_name()
    _patch_sandbox_resources()
    _patch_call_tool_image_responses()
    from mcp_openapi_proxy import main as proxy_main

    proxy_main()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit(1)