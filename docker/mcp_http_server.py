"""Run the Hermes MCP server over streamable HTTP instead of stdio.

Wires ``mcp_serve.create_mcp_server()`` (which builds the same FastMCP
instance with the same 10 tools) to FastMCP's ``run_streamable_http_async``
transport, so the server is reachable as ``http://host:port/mcp`` from any
container on the network.

Why a separate entrypoint from ``mcp_serve.run_mcp_server``: that one binds
the server to stdio, which the container's stdio pair already owns (tini +
entrypoint + foreground ``gateway run``). Docker doesn't multiplex stdio,
so the SDK can't reach MCP that way from the host. Streamable HTTP puts
MCP on its own TCP port (default 8000), leaving the gateway's HTTP API
(8642) and stdio untouched. Same server, same tools — transport only.

Configured via env vars (all optional):

    HERMES_MCP_HTTP_HOST   default: 0.0.0.0
    HERMES_MCP_HTTP_PORT   default: 8000
    HERMES_MCP_HTTP_PATH   default: /mcp

This is invoked as a side-process by ``docker/entrypoint.sh`` when
``HERMES_MCP_HTTP_PORT`` is set, alongside the main ``hermes`` foreground
process. Not intended to be invoked directly by users.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys


def main() -> None:
    host = os.environ.get("HERMES_MCP_HTTP_HOST", "0.0.0.0")
    port = int(os.environ.get("HERMES_MCP_HTTP_PORT", "8000"))
    path = os.environ.get("HERMES_MCP_HTTP_PATH", "/mcp")

    verbose = os.environ.get("HERMES_MCP_VERBOSE", "").lower() in ("1", "true", "yes")
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        stream=sys.stderr,
        format="[mcp-http] %(message)s",
    )

    try:
        from mcp_serve import EventBridge, create_mcp_server, _MCP_SERVER_AVAILABLE
    except ImportError as e:
        print(f"[mcp-http] failed to import mcp_serve: {e}", file=sys.stderr)
        sys.exit(1)

    if not _MCP_SERVER_AVAILABLE:
        print("[mcp-http] mcp package not installed in this venv", file=sys.stderr)
        sys.exit(1)

    bridge = EventBridge()
    bridge.start()

    server = create_mcp_server(event_bridge=bridge)
    server.settings.host = host
    server.settings.port = port
    server.settings.streamable_http_path = path

    logging.info("listening on http://%s:%d%s", host, port, path)

    async def _run() -> None:
        try:
            await server.run_streamable_http_async()
        finally:
            bridge.stop()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        bridge.stop()


if __name__ == "__main__":
    main()
