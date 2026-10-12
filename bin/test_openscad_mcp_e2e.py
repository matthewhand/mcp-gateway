#!/usr/bin/env python3
"""Comprehensive E2E: OpenSCAD image paths through the real MCP gateway.

Talks **MCP streamable HTTP** (same transport as production ``mcp-cli-server@openscad``):

  client  →  supergateway (:8816)  →  mcp_openapi_proxy_cli  →  cli_bridge (:3860)
          ↘  public HTTPS /sandbox/openscad/… (nginx → bridge static)

Covers:

1. MCP session lifecycle (initialize → notifications/initialized → tools/list)
2. **Default path** — ``render_png`` with ``return_inline`` omitted/true:
   ImageContent (base64) **and** ResourceLink + public URL; file saved to sandbox
3. **Public host** — GET the returned HTTPS URL (nginx, unauthenticated) → valid PNG
4. **Bridge static** — GET ``http://cli_bridge/sandbox/…`` → same PNG
5. **URL-only path** — ``return_inline=false``: ResourceLink only, no ImageContent
6. **JSON-RPC CallToolResult image+link contract** — full envelope
   ``{jsonrpc,id,result:{content:[{type:image,…},{type:resource_link,…},…],isError:false}}``
   with raw base64 (no data-URI prefix) that decodes to a real PNG
7. **JSON-RPC CallToolResult URL-only contract** — full envelope with ``resource_link``
   (no ``type:image``) when ``return_inline=false``
8. **MCP resources** — resources/list + resources/read ``sandbox://…`` returns blob
9. **download_file** tool and cleanup via delete_file

Environment:

  MCP_E2E_URL          default http://127.0.0.1:8816/mcp
  MCP_E2E_CLI_URL      default http://127.0.0.1:3860  (bridge static / openapi)
  MCP_E2E_SELF_START=1 spin isolated bridge+supergateway on free ports (slow first run)
  MCP_E2E_SKIP_PUBLIC=1 skip HTTPS public URL fetch (offline / no nginx)
  MCP_PUBLIC_URL        used when self-starting (default http://127.0.0.1:<cli_port>)

Run:

  python3 bin/test_openscad_mcp_e2e.py
  MCP_E2E_SELF_START=1 python3 bin/test_openscad_mcp_e2e.py
"""
from __future__ import annotations

import base64
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
SCAD_CUBE = "cube([10, 10, 10], center=true);"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# MCP streamable-HTTP client
# ---------------------------------------------------------------------------


@dataclass
class McpClient:
    url: str
    session_id: str | None = None
    _id: int = 0
    timeout: float = 180.0

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        notification: bool = False,
        full_message: bool = False,
        request_id: int | None = None,
    ) -> Any:
        """POST a JSON-RPC message. By default returns ``result``; set
        ``full_message=True`` to get the whole envelope (jsonrpc/id/result)."""
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        rid: int | None = None
        if not notification:
            rid = self._next_id() if request_id is None else request_id
            body["id"] = rid
        if params is not None:
            body["params"] = params
        data = json.dumps(body).encode()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        req = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                sid = resp.headers.get("mcp-session-id")
                if sid:
                    self.session_id = sid
                status = resp.status
                raw = resp.read().decode()
                ctype = resp.headers.get("content-type", "")
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode(errors="replace")
            raise AssertionError(f"MCP HTTP {exc.code} for {method}: {err_body[:500]}") from exc

        if notification or status == 202 or not raw.strip():
            return None

        messages = _parse_mcp_body(raw, ctype)
        if not messages:
            raise AssertionError(f"empty MCP response for {method}: {raw[:300]!r}")
        # Prefer response matching our id when multiple frames
        msg = messages[-1]
        if rid is not None:
            for candidate in messages:
                if candidate.get("id") == rid:
                    msg = candidate
                    break
        if "error" in msg:
            raise AssertionError(f"MCP error for {method}: {msg['error']}")
        if full_message:
            return msg
        return msg.get("result")

    def initialize(self) -> dict[str, Any]:
        result = self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "openscad-mcp-e2e", "version": "1.0"},
            },
        )
        assert result is not None
        self.request("notifications/initialized", notification=True)
        return result

    def tools_list(self) -> list[dict[str, Any]]:
        result = self.request("tools/list", {})
        return list(result["tools"])

    def tools_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        assert result is not None
        return result

    def tools_call_full(self, name: str, arguments: dict[str, Any], *, request_id: int = 42) -> dict[str, Any]:
        """tools/call returning the full JSON-RPC 2.0 envelope (for contract tests)."""
        msg = self.request(
            "tools/call",
            {"name": name, "arguments": arguments},
            full_message=True,
            request_id=request_id,
        )
        assert msg is not None
        return msg

    def resources_list(self) -> list[dict[str, Any]]:
        result = self.request("resources/list", {})
        return list(result.get("resources") or [])

    def resources_read(self, uri: str) -> dict[str, Any]:
        result = self.request("resources/read", {"uri": uri})
        assert result is not None
        return result


def _parse_mcp_body(raw: str, ctype: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if "text/event-stream" in ctype or raw.lstrip().startswith("event:") or "\ndata:" in raw:
        for block in raw.split("\n\n"):
            for line in block.splitlines():
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if payload:
                        messages.append(json.loads(payload))
    else:
        messages.append(json.loads(raw))
    return messages


def http_get(url: str, timeout: float = 30.0) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_http(url: str, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status < 500:
                    return
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.25)
    raise TimeoutError(f"timeout waiting for {url}: {last}")


# ---------------------------------------------------------------------------
# Optional self-start stack (bridge + supergateway + patched proxy)
# ---------------------------------------------------------------------------


@dataclass
class SelfStartedStack:
    cli_port: int
    mcp_port: int
    public_url: str
    procs: list[subprocess.Popen] = field(default_factory=list)
    log_path: Path | None = None

    @property
    def mcp_url(self) -> str:
        return f"http://127.0.0.1:{self.mcp_port}/mcp"

    @property
    def cli_base(self) -> str:
        return f"http://127.0.0.1:{self.cli_port}"

    def stop(self) -> None:
        for p in reversed(self.procs):
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.time() + 8
        for p in self.procs:
            while p.poll() is None and time.time() < deadline:
                time.sleep(0.1)
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def _wait_tcp(host: str, port: int, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.2)
    raise TimeoutError(f"timeout waiting for {host}:{port}: {last}")


def start_isolated_stack() -> SelfStartedStack:
    """Start cli_bridge + supergateway on free ports (does not touch prod 8816/3860)."""
    cli_port = free_port()
    mcp_port = free_port()
    public = os.environ.get("MCP_PUBLIC_URL") or f"http://127.0.0.1:{cli_port}"
    log_path = Path(os.environ.get("MCP_E2E_LOG", "/tmp/openscad-mcp-e2e.log"))
    logf = open(log_path, "w")  # noqa: SIM115

    # Pre-warm uvx/mcp-openapi-proxy so first MCP initialize is not racing cold start
    warm = subprocess.run(
        [
            "uvx",
            "--with",
            "mcp-openapi-proxy~=0.3.3",
            "--",
            "python",
            "-c",
            "import mcp_openapi_proxy; print('warm')",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(ROOT),
    )
    if warm.returncode != 0:
        logf.write(f"uvx warm failed: {warm.stderr}\n")
        logf.flush()

    bridge_env = os.environ.copy()
    bridge_env["MCP_PUBLIC_URL"] = public
    bridge_cmd = [
        sys.executable,
        str(ROOT / "bin" / "cli_bridge.py"),
        "serve",
        "--config",
        str(ROOT / "configs" / "openscad.json"),
        "--port",
        str(cli_port),
    ]
    bridge = subprocess.Popen(
        bridge_cmd,
        env=bridge_env,
        stdout=logf,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(ROOT),
    )
    wait_http(f"http://127.0.0.1:{cli_port}/openapi.json", timeout=30)

    # Match launch-cli-server.sh env + process model
    proxy_env = os.environ.copy()
    proxy_env.update(
        {
            "HOME": os.environ.get("HOME", "/home/chatgpt"),
            "PATH": os.environ.get(
                "PATH",
                "/home/chatgpt/.local/bin:/usr/local/bin:/usr/bin:/bin",
            ),
            "OPENAPI_SPEC_URL": f"http://127.0.0.1:{cli_port}/openapi.json",
            "SERVER_URL_OVERRIDE": f"http://127.0.0.1:{cli_port}",
            "TOOL_WHITELIST": "/render,/files,/version,/run",
            "TOOL_NAME_USE_OPERATION_ID": "true",
            "CAPABILITIES_RESOURCES": "true",
            "MCP_PUBLIC_URL": public,
        }
    )
    stdio_cmd = (
        f"uvx --with 'mcp-openapi-proxy~=0.3.3' -- "
        f"python {ROOT / 'bin' / 'mcp_openapi_proxy_cli.py'}"
    )
    sg_cmd = [
        "npx",
        "-y",
        "supergateway",
        "--stdio",
        stdio_cmd,
        "--outputTransport",
        "streamableHttp",
        "--port",
        str(mcp_port),
        "--logLevel",
        "info",
        "--stateful",
        "--sessionTimeout",
        "3600000",
    ]
    sg = subprocess.Popen(
        sg_cmd,
        env=proxy_env,
        stdout=logf,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(ROOT),
    )
    stack = SelfStartedStack(
        cli_port=cli_port,
        mcp_port=mcp_port,
        public_url=public.rstrip("/"),
        procs=[bridge, sg],
        log_path=log_path,
    )
    # Only wait for TCP + "Listening" — do NOT initialize here.
    # Cold first initialize while probing races stateful streamable HTTP and
    # can crash supergateway ("No connection established for request ID").
    deadline = time.time() + 90
    while time.time() < deadline:
        if bridge.poll() is not None or sg.poll() is not None:
            logf.flush()
            raise RuntimeError(
                f"stack exited early bridge={bridge.poll()} sg={sg.poll()} log={log_path}"
            )
        try:
            _wait_tcp("127.0.0.1", mcp_port, timeout=1)
            # brief settle so child stdio proxy can register tools
            time.sleep(1.5)
            if sg.poll() is not None:
                continue
            return stack
        except Exception:
            time.sleep(0.3)
    raise TimeoutError(f"MCP stack not ready on :{mcp_port} (log {log_path})")


# ---------------------------------------------------------------------------
# Assertions / helpers
# ---------------------------------------------------------------------------


def assert_png(data: bytes, *, label: str) -> None:
    assert data.startswith(PNG_MAGIC), f"{label}: not a PNG ({data[:16]!r})"
    assert len(data) > 50, f"{label}: PNG too small ({len(data)} bytes)"


def content_by_type(result: dict[str, Any], ctype: str) -> list[dict[str, Any]]:
    return [c for c in result.get("content") or [] if c.get("type") == ctype]


def dump_fail(result: dict[str, Any]) -> str:
    return json.dumps(result, indent=2)[:2000]


# ---------------------------------------------------------------------------
# E2E cases
# ---------------------------------------------------------------------------


@dataclass
class E2EContext:
    client: McpClient
    cli_base: str
    skip_public: bool = False
    tag: str = field(default_factory=lambda: f"e2e_{int(time.time())}")


def test_mcp_initialize_and_tools(ctx: E2EContext) -> None:
    init = ctx.client.initialize()
    caps = init.get("capabilities") or {}
    assert "tools" in caps, f"missing tools capability: {init}"
    tools = {t["name"] for t in ctx.client.tools_list()}
    for required in (
        "render_png",
        "render_stl",
        "upload_file",
        "download_file",
        "list_files",
        "delete_file",
        "version",
    ):
        assert required in tools, f"missing tool {required}; have {sorted(tools)}"


def test_default_path_image_and_resource_link(ctx: E2EContext) -> dict[str, Any]:
    """Default: ImageContent (base64) AND ResourceLink; file saved to sandbox."""
    name = f"{ctx.tag}_default.png"
    result = ctx.client.tools_call(
        "render_png",
        {
            "scad": SCAD_CUBE,
            "imgsize": "200,200",
            "autocenter": True,
            "viewall": True,
            # return_inline omitted → default true
            "output_name": name,
        },
    )
    assert not result.get("isError"), dump_fail(result)

    images = content_by_type(result, "image")
    links = content_by_type(result, "resource_link")
    texts = content_by_type(result, "text")
    assert images, f"default path must return ImageContent: {dump_fail(result)}"
    assert links, f"default path must return resource_link: {dump_fail(result)}"
    assert texts, f"default path must return text: {dump_fail(result)}"

    img = images[0]
    assert img.get("mimeType") == "image/png", img
    b64 = img.get("data") or ""
    assert b64 and not b64.startswith("data:"), b64[:40]
    assert_png(base64.b64decode(b64), label="default ImageContent")

    uri = str(links[0].get("uri") or "")
    assert uri, f"resource_link missing uri: {links[0]}"
    assert name in uri or name in str(links[0].get("name")), links[0]
    assert links[0].get("mimeType") == "image/png", links[0]

    sc = result.get("structuredContent") or {}
    of = sc.get("output_file") or {}
    assert sc.get("url") or of.get("url") or uri, sc
    assert of.get("data_uri", "").startswith("data:image/png;base64,") or b64, of
    assert "base64" not in of, of.keys()
    assert of.get("filename") == name or sc.get("filename") == name, sc

    public_url = sc.get("url") or of.get("url") or uri
    return {
        "filename": name,
        "public_url": public_url,
        "png_bytes": base64.b64decode(b64),
        "result": result,
    }


def test_url_only_path_via_mcp_tools_call(ctx: E2EContext) -> dict[str, Any]:
    """return_inline=false: ResourceLink + public URL, no base64 ImageContent."""
    name = f"{ctx.tag}_url.png"
    result = ctx.client.tools_call(
        "render_png",
        {
            "scad": SCAD_CUBE,
            "imgsize": "200,200",
            "autocenter": True,
            "viewall": True,
            "return_inline": False,
            "output_name": name,
        },
    )
    assert not result.get("isError"), dump_fail(result)

    images = content_by_type(result, "image")
    links = content_by_type(result, "resource_link")
    texts = content_by_type(result, "text")
    assert not images, f"URL-only path must not return ImageContent: {dump_fail(result)}"
    assert links, f"URL-only path must return resource_link: {dump_fail(result)}"
    assert texts, f"URL-only path must return text: {dump_fail(result)}"

    uri = str(links[0].get("uri") or "")
    assert uri, f"resource_link missing uri: {links[0]}"
    assert name in uri or name in str(links[0].get("name")), links[0]
    text = texts[0].get("text") or ""
    assert "![OpenSCAD preview](" in text, text[:400]
    assert uri in text or "Preview URL:" in text, text[:400]
    assert "data:image/png;base64," not in text, "URL-only text must not embed data URI"

    sc = result.get("structuredContent") or {}
    assert sc.get("url") or (sc.get("output_file") or {}).get("url"), sc
    of = sc.get("output_file") or {}
    assert "base64" not in of, f"structuredContent must not carry base64: {of.keys()}"
    assert of.get("filename") == name or sc.get("filename") == name, sc

    public_url = sc.get("url") or of.get("url") or uri
    return {"filename": name, "public_url": public_url, "result": result}


def test_public_and_bridge_static_host(ctx: E2EContext, url_meta: dict[str, Any]) -> None:
    """Unauthenticated static hosting: public HTTPS (nginx) + local bridge /sandbox/."""
    filename = url_meta["filename"]
    public_url = str(url_meta["public_url"])

    # Bridge static (always available when cli_base is the bridge)
    local_url = f"{ctx.cli_base.rstrip('/')}/sandbox/{filename}"
    code, headers, body = http_get(local_url)
    assert code == 200, f"bridge static {local_url} → {code}"
    assert "image/png" in headers.get("content-type", ""), headers
    assert_png(body, label="bridge static")

    # Public URL (nginx → bridge). Production nginx:
    #   location /sandbox/openscad/ { proxy_pass http://127.0.0.1:3860/sandbox/; }
    # so the advertised URL includes the slug; the bridge itself does not.
    if ctx.skip_public:
        return
    parsed = urlparse(public_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return

    code, headers, pub_body = http_get(public_url)
    if code == 404 and "/sandbox/" in parsed.path:
        # Self-start without nginx: strip /sandbox/<slug>/ → /sandbox/ (same rewrite nginx does)
        parts = parsed.path.split("/")
        # ['', 'sandbox', 'openscad', 'file.png'] → /sandbox/file.png
        if len(parts) >= 4 and parts[1] == "sandbox":
            stripped = "/".join(["", "sandbox", *parts[3:]])
            alt = f"{parsed.scheme}://{parsed.netloc}{stripped}"
            if parsed.query:
                alt += f"?{parsed.query}"
            code, headers, pub_body = http_get(alt)
            assert code == 200, (
                f"public URL {public_url} → 404; nginx-style rewrite {alt} → {code}"
            )
        else:
            assert False, f"public URL {public_url} → {code}"
    else:
        assert code == 200, f"public URL {public_url} → {code}"

    assert "www-authenticate" not in headers, headers
    assert "image/png" in headers.get("content-type", ""), headers
    assert_png(pub_body, label="public URL")
    assert pub_body == body, "public vs bridge PNG mismatch"


def test_jsonrpc_calltoolresult_image_and_link_contract(ctx: E2EContext) -> dict[str, Any]:
    """Prove the live MCP default wire shape: ImageContent + ResourceLink.

    At the JSON-RPC layer (payload inside streamable-HTTP SSE ``data:`` frames)::

        {
          "jsonrpc": "2.0",
          "id": 42,
          "result": {
            "content": [
              {"type": "image", "data": "<raw-base64-png>", "mimeType": "image/png"},
              {"type": "resource_link", "uri": "https://…/sandbox/openscad/…", …},
              {"type": "text", "text": "..."}
            ],
            "isError": false
          }
        }

    ``data`` is raw base64 of PNG bytes — no ``data:`` URI prefix.
    """
    name = f"{ctx.tag}_contract.png"
    req_id = 42
    envelope = ctx.client.tools_call_full(
        "render_png",
        {
            "scad": SCAD_CUBE,
            "imgsize": "120,120",
            "autocenter": True,
            "viewall": True,
            # default return_inline=true
            "output_name": name,
        },
        request_id=req_id,
    )

    # --- outer JSON-RPC 2.0 envelope ---
    assert envelope.get("jsonrpc") == "2.0", envelope
    assert envelope.get("id") == req_id, envelope
    assert "error" not in envelope, f"protocol error (not tool error): {envelope}"
    assert "result" in envelope, envelope
    assert set(envelope.keys()) <= {"jsonrpc", "id", "result"}, set(envelope.keys())

    result = envelope["result"]
    assert isinstance(result, dict), result
    assert result.get("isError") is False, dump_fail(result)
    content = result.get("content")
    assert isinstance(content, list) and content, dump_fail(result)

    # --- ImageContent block ---
    images = [c for c in content if c.get("type") == "image"]
    assert len(images) >= 1, f"expected type=image block: {dump_fail(result)}"
    img = images[0]
    assert set(img.keys()) >= {"type", "data", "mimeType"}, img
    assert img["type"] == "image"
    assert img["mimeType"] == "image/png"
    b64 = img["data"]
    assert isinstance(b64, str) and b64, "image.data must be non-empty base64 string"
    assert not b64.startswith("data:"), "image.data must be raw base64, not a data URI"
    assert b64.startswith("iVBORw0KGgo"), f"expected PNG base64 prefix, got {b64[:24]!r}"
    raw = base64.b64decode(b64, validate=False)
    assert_png(raw, label="contract ImageContent.data")
    assert base64.b64decode(b64) == raw

    # --- ResourceLink block (saved artifact) ---
    links = [c for c in content if c.get("type") == "resource_link"]
    assert links, f"expected resource_link alongside image: {dump_fail(result)}"
    link = links[0]
    assert link.get("mimeType") == "image/png", link
    assert isinstance(link.get("uri"), str) and link["uri"], link
    assert "data" not in link, link

    texts = [c for c in content if c.get("type") == "text"]
    assert texts, f"expected accompanying text block: {dump_fail(result)}"
    assert isinstance(texts[0].get("text"), str) and texts[0]["text"], texts[0]
    assert "isError" in result

    return {"filename": name, "envelope": envelope, "png_bytes": raw, "public_url": link["uri"]}


def test_jsonrpc_calltoolresult_url_only_contract(ctx: E2EContext) -> dict[str, Any]:
    """return_inline=false: full JSON-RPC envelope with resource_link (not image)."""
    name = f"{ctx.tag}_url_contract.png"
    req_id = 43
    envelope = ctx.client.tools_call_full(
        "render_png",
        {
            "scad": SCAD_CUBE,
            "imgsize": "100,100",
            "autocenter": True,
            "viewall": True,
            "return_inline": False,
            "output_name": name,
        },
        request_id=req_id,
    )
    assert envelope.get("jsonrpc") == "2.0"
    assert envelope.get("id") == req_id
    assert "error" not in envelope
    result = envelope["result"]
    assert result.get("isError") is False
    types_present = [c.get("type") for c in result.get("content") or []]
    assert "image" not in types_present, types_present
    assert "resource_link" in types_present, types_present
    link = next(c for c in result["content"] if c.get("type") == "resource_link")
    assert link.get("mimeType") == "image/png"
    assert isinstance(link.get("uri"), str) and link["uri"]
    assert "data" not in link, link
    return {"filename": name, "envelope": envelope}


def test_resources_list_and_read(ctx: E2EContext, filenames: list[str]) -> None:
    resources = ctx.client.resources_list()
    uris = {r.get("uri") for r in resources}
    for name in filenames:
        uri = f"sandbox://{name}"
        assert uri in uris, f"{uri} not in resources/list ({sorted(uris)[:20]}…)"

    for name in filenames:
        uri = f"sandbox://{name}"
        read = ctx.client.resources_read(uri)
        contents = read.get("contents") or []
        assert contents, f"resources/read empty for {uri}: {read}"
        blob_b64 = contents[0].get("blob")
        assert blob_b64, f"no blob for {uri}: {contents[0].keys()}"
        assert_png(base64.b64decode(blob_b64), label=f"resources/read {uri}")
        assert contents[0].get("mimeType") == "image/png"


def test_download_file_tool(ctx: E2EContext, filename: str) -> None:
    result = ctx.client.tools_call("download_file", {"filepath": filename})
    assert not result.get("isError"), dump_fail(result)
    # download embeds base64 via bridge; proxy may expand to image or text
    images = content_by_type(result, "image")
    if images:
        assert_png(base64.b64decode(images[0]["data"]), label="download_file image")
    else:
        sc = result.get("structuredContent") or {}
        # some proxy versions put payload only in content text JSON — accept either
        of = sc.get("output_file") if isinstance(sc.get("output_file"), dict) else None
        if of and of.get("data_uri"):
            assert of["data_uri"].startswith("data:image/png;base64,")
        else:
            # fallback: at least not an error and mentions filename
            texts = content_by_type(result, "text")
            joined = " ".join(t.get("text") or "" for t in texts)
            assert filename in joined or images, dump_fail(result)


def test_cleanup_delete(ctx: E2EContext, filenames: list[str]) -> None:
    for name in filenames:
        result = ctx.client.tools_call("delete_file", {"filepath": name})
        assert not result.get("isError"), dump_fail(result)
        code, _, _ = http_get(f"{ctx.cli_base.rstrip('/')}/sandbox/{name}")
        assert code == 404, f"expected 404 after delete {name}, got {code}"


def test_version_tool(ctx: E2EContext) -> None:
    result = ctx.client.tools_call("version", {})
    assert not result.get("isError"), dump_fail(result)
    texts = content_by_type(result, "text")
    joined = " ".join(t.get("text") or "" for t in texts).lower()
    # bridge returns JSON with stdout containing version string
    assert "openscad" in joined or "version" in joined or result.get("structuredContent"), dump_fail(
        result
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def resolve_endpoints() -> tuple[str, str, SelfStartedStack | None]:
    """Return (mcp_url, cli_base, optional stack to stop)."""
    if os.environ.get("MCP_E2E_SELF_START", "").strip() in ("1", "true", "yes"):
        print("self-starting isolated bridge + supergateway…", flush=True)
        stack = start_isolated_stack()
        print(
            f"  mcp={stack.mcp_url} cli={stack.cli_base} public={stack.public_url}",
            flush=True,
        )
        return stack.mcp_url, stack.cli_base, stack

    mcp_url = os.environ.get("MCP_E2E_URL", "http://127.0.0.1:8816/mcp")
    cli_base = os.environ.get("MCP_E2E_CLI_URL", "http://127.0.0.1:3860")
    # probe live
    try:
        wait_http(f"{cli_base.rstrip('/')}/openapi.json", timeout=5)
        client = McpClient(url=mcp_url, timeout=15)
        client.initialize()
        print(f"using live gateway mcp={mcp_url} cli={cli_base}", flush=True)
        return mcp_url, cli_base, None
    except Exception as exc:
        print(f"live gateway unavailable ({exc}); self-starting…", flush=True)
        stack = start_isolated_stack()
        print(
            f"  mcp={stack.mcp_url} cli={stack.cli_base} public={stack.public_url}",
            flush=True,
        )
        return stack.mcp_url, stack.cli_base, stack


def main() -> int:
    mcp_url, cli_base, stack = resolve_endpoints()
    skip_public = os.environ.get("MCP_E2E_SKIP_PUBLIC", "").strip() in ("1", "true", "yes")
    # when self-started with local public URL, public fetch still works (same host)
    client = McpClient(url=mcp_url, timeout=180)
    ctx = E2EContext(client=client, cli_base=cli_base, skip_public=skip_public)

    cases: list[tuple[str, Any]] = []
    failed = 0
    created: list[str] = []

    def run(name: str, fn, *args):
        nonlocal failed
        try:
            out = fn(*args)
            print(f"  ok  {name}", flush=True)
            return out
        except Exception as exc:
            failed += 1
            print(f"FAIL  {name}: {exc}", flush=True)
            raise

    case_names: list[str] = []

    try:
        case_names.append("mcp_initialize_and_tools")
        run(case_names[-1], test_mcp_initialize_and_tools, ctx)
        case_names.append("version_tool")
        run(case_names[-1], test_version_tool, ctx)

        case_names.append("default_path_image_and_resource_link")
        default_meta = run(case_names[-1], test_default_path_image_and_resource_link, ctx)
        created.append(default_meta["filename"])
        case_names.append("public_and_bridge_static_host")
        run(case_names[-1], test_public_and_bridge_static_host, ctx, default_meta)

        case_names.append("url_only_path_via_mcp_tools_call")
        url_meta = run(case_names[-1], test_url_only_path_via_mcp_tools_call, ctx)
        created.append(url_meta["filename"])

        # Explicit JSON-RPC CallToolResult contracts (default image+link vs URL-only)
        case_names.append("jsonrpc_calltoolresult_image_and_link_contract")
        contract = run(case_names[-1], test_jsonrpc_calltoolresult_image_and_link_contract, ctx)
        created.append(contract["filename"])
        env_proof = {
            "jsonrpc": contract["envelope"]["jsonrpc"],
            "id": contract["envelope"]["id"],
            "result": {
                "content": [
                    {
                        "type": c.get("type"),
                        **(
                            {
                                "data": (c.get("data") or "")[:48]
                                + f"...[{len(c.get('data') or '')} chars]",
                                "mimeType": c.get("mimeType"),
                            }
                            if c.get("type") == "image"
                            else {k: c.get(k) for k in c if k != "type"}
                        ),
                    }
                    for c in (contract["envelope"]["result"].get("content") or [])
                ],
                "isError": contract["envelope"]["result"].get("isError"),
            },
        }
        print("  proof default envelope:", json.dumps(env_proof, indent=2)[:1500], flush=True)

        case_names.append("jsonrpc_calltoolresult_url_only_contract")
        url_contract = run(case_names[-1], test_jsonrpc_calltoolresult_url_only_contract, ctx)
        created.append(url_contract["filename"])

        case_names.append("resources_list_and_read")
        run(case_names[-1], test_resources_list_and_read, ctx, created)
        case_names.append("download_file_tool")
        run(case_names[-1], test_download_file_tool, ctx, created[0])
        case_names.append("cleanup_delete")
        run(case_names[-1], test_cleanup_delete, ctx, list(created))
    finally:
        if stack is not None:
            stack.stop()
            print("stopped self-started stack", flush=True)

    if failed:
        print(f"\n{failed} failure(s)", flush=True)
        return 1
    print(f"\nall {len(case_names)} e2e cases passed (mcp={mcp_url})", flush=True)
    print("ok")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"\ne2e aborted: {exc}", file=sys.stderr)
        raise
