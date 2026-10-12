#!/usr/bin/env python3
"""Gateway entry for stock mcp-openapi-proxy with query-param auth and
outbound-request timeouts.

mcp-openapi-proxy only attaches API_KEY as HTTP headers (Bearer / api-key /
custom Authorization scheme). Wolfram LLM API (and similar) need:

  API_AUTH_TYPE=query
  API_AUTH_PARAM=appid
  API_KEY=<secret>

This entrypoint injects that query parameter on every tool call (clients only
pass business args like ``input``) and suppresses the bogus
``Authorization: query <key>`` header the stock package would otherwise send.

It also hardens the tool-call path: upstream calls in
``mcp_openapi_proxy.server_lowlevel.dispatcher_handler`` are issued with
``requests.request(...)`` *without* a ``timeout`` and are a **blocking** call
made from inside an ``async def``. The proxy runs a single uvicorn worker, so
one upstream that accepts the TCP connection but never responds parks the one
event loop in ``recv()`` forever. The port stays LISTEN, the MCP ``initialize``
handshake times out for every client (the request is queued in the kernel
backlog and never read), and only a restart recovers it. This is the wedge
recorded in issue #34.

We fix it here rather than by editing the installed package so the fix survives
package upgrades:

  * inject a default ``(connect, read)`` timeout into every ``requests`` call
    made from the dispatcher module, so a hung upstream raises
    ``RequestException`` (already handled upstream) instead of hanging forever;
  * run the blocking call in a worker thread so a slow-but-alive upstream
    degrades one request instead of stalling the whole event loop.
"""
from __future__ import annotations

import os
from typing import Any

# Outbound tool-call timeouts. Generous by default so long-running tools
# (e.g. shell builds) still succeed, but bounded so the event loop can never
# be pinned indefinitely. Override per-deployment via env.
_DEFAULT_CONNECT_TIMEOUT = float(os.getenv("MCP_PROXY_CONNECT_TIMEOUT", "10"))
_DEFAULT_READ_TIMEOUT = float(os.getenv("MCP_PROXY_READ_TIMEOUT", "120"))


def _truthy_query_auth() -> bool:
    return os.getenv("API_AUTH_TYPE", "Bearer").strip().lower() == "query"


def _query_auth_pair() -> tuple[str, str] | None:
    if not _truthy_query_auth():
        return None
    key = os.getenv("API_KEY") or ""
    param = (os.getenv("API_AUTH_PARAM") or "appid").strip()
    if not key or not param:
        return None
    return param, key


def _inject_query_auth_args(arguments: Any) -> dict[str, Any]:
    """Return tool arguments with server-side query auth merged in."""
    base: dict[str, Any] = dict(arguments) if isinstance(arguments, dict) else {}
    pair = _query_auth_pair()
    if not pair:
        return base
    param, key = pair
    # Prefer server key so clients never need (or accidentally override) the secret.
    base[param] = key
    return base


def _patch_handle_auth_skip_query() -> None:
    """Do not put API_AUTH_TYPE=query into an Authorization header."""
    import mcp_openapi_proxy.openapi as openapi
    import mcp_openapi_proxy.utils as utils

    for mod in (openapi, utils):
        original = mod.handle_auth

        def handle_auth(operation, _original=original):  # type: ignore[no-untyped-def]
            if _truthy_query_auth():
                return {}
            return _original(operation)

        mod.handle_auth = handle_auth  # type: ignore[assignment]


class _TimeoutRequestsShim:
    """Proxy object that adds a default timeout to bare ``requests`` calls.

    Only ``request`` is intercepted; attribute access otherwise falls through to
    the real ``requests`` module, so ``requests.exceptions`` (used in the
    dispatcher's ``except`` clause) keeps working.
    """

    def __init__(self, real_module: Any) -> None:
        self._real = real_module

    def request(self, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = (_DEFAULT_CONNECT_TIMEOUT, _DEFAULT_READ_TIMEOUT)
        return self._real.request(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - passthrough
        return getattr(self._real, name)


def _patch_dispatcher_request_timeout() -> None:
    """Give every outbound tool-call request a bounded timeout.

    Applied by rebinding the ``requests`` name inside the dispatcher module, so
    the single ``requests.request(...)`` call site there (and any future one)
    cannot hang forever. The blocking call still runs on the event loop, but it
    is now guaranteed to return, so the loop recovers on its own.
    """
    import mcp_openapi_proxy.server_lowlevel as server_lowlevel

    real_requests = getattr(server_lowlevel, "requests", None)
    if real_requests is None:  # pragma: no cover - upstream layout changed
        return
    if isinstance(real_requests, _TimeoutRequestsShim):
        return  # already patched
    server_lowlevel.requests = _TimeoutRequestsShim(real_requests)  # type: ignore[assignment]


def _patch_dispatcher_query_auth() -> None:
    """Inject API_KEY into tool-call arguments before the stock dispatcher runs."""
    import mcp_openapi_proxy.server_lowlevel as server_lowlevel
    from mcp_openapi_proxy.logging_setup import logger

    original = server_lowlevel.dispatcher_handler

    async def dispatcher_handler(ctx_or_request: Any, params: Any = None) -> Any:
        from mcp_openapi_proxy.protocol import unpack_handler_args

        ctx, params = unpack_handler_args(ctx_or_request, params)
        pair = _query_auth_pair()
        if pair is None:
            return await original(ctx, params)

        new_args = _inject_query_auth_args(getattr(params, "arguments", None))
        try:
            params = params.model_copy(update={"arguments": new_args})
        except Exception:
            try:
                params.arguments = new_args  # type: ignore[attr-defined]
            except Exception as exc:
                logger.warning("query auth inject failed: %s", exc)
                return await original(ctx, params)

        param_name, _ = pair
        logger.debug("Injected query auth param %r from API_KEY", param_name)
        return await original(ctx, params)

    server_lowlevel.dispatcher_handler = dispatcher_handler

    # FastMCP simple mode path (rarely used by this gateway)
    try:
        import mcp_openapi_proxy.server_fastmcp as server_fastmcp

        original_cf = server_fastmcp.call_function

        def call_function(*, function_name, parameters=None, env_key="OPENAPI_SPEC_URL"):  # type: ignore[no-untyped-def]
            return original_cf(
                function_name=function_name,
                parameters=_inject_query_auth_args(parameters),
                env_key=env_key,
            )

        server_fastmcp.call_function = call_function  # type: ignore[assignment]
    except Exception:
        pass


def main() -> None:
    _patch_handle_auth_skip_query()
    _patch_dispatcher_query_auth()
    # Hardening: bound every outbound tool call so a hung upstream can never
    # wedge the single-worker event loop (issue #34).
    try:
        _patch_dispatcher_request_timeout()
    except Exception:
        import traceback

        traceback.print_exc(file=__import__("sys").stderr)
    from mcp_openapi_proxy import main as proxy_main

    proxy_main()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback, sys
        traceback.print_exc(file=sys.stderr)
        raise SystemExit(1)
