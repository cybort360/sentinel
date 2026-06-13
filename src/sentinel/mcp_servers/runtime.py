"""Shared transport selection for the three MCP servers (architecture.md §5, §15).

Local development and the unit-test harness drive each FastMCP server over
**stdio** — the default — so an MCP client (or the orchestrator) speaks to it on
stdin/stdout. The docker-compose deployment (§15) instead needs each server to be
a long-lived *networked* service, so it sets ``SENTINEL_MCP_TRANSPORT=streamable-http``
(plus ``SENTINEL_MCP_HOST``/``SENTINEL_MCP_PORT``) and the same ``main()`` brings
the server up as an HTTP endpoint. Keeping the default at stdio means nothing in
the existing code paths or tests changes.

This binds only the MCP server's own HTTP port; it has nothing to do with the
Rule 3 broadcast-RPC boundary (which is enforced separately in
``simulation_mcp.config.assert_local_rpc``).
"""

from __future__ import annotations

import os
from typing import Literal

from mcp.server.fastmcp import FastMCP

Transport = Literal["stdio", "streamable-http", "sse"]

_VALID: dict[str, Transport] = {
    "stdio": "stdio",
    "streamable-http": "streamable-http",
    "sse": "sse",
}


def transport_from_env() -> Transport:
    """Return the configured MCP transport (``SENTINEL_MCP_TRANSPORT``, default stdio).

    Returns:
        The selected transport literal.

    Raises:
        ValueError: If the env var names a transport FastMCP does not support.
    """
    raw = os.environ.get("SENTINEL_MCP_TRANSPORT", "stdio").strip()
    try:
        return _VALID[raw]
    except KeyError:
        raise ValueError(
            f"unknown SENTINEL_MCP_TRANSPORT {raw!r}; expected one of {sorted(_VALID)}"
        ) from None


def run_server(server: FastMCP) -> None:
    """Run a FastMCP server using the env-selected transport (architecture.md §15).

    For any networked transport, the bind host/port are read from
    ``SENTINEL_MCP_HOST`` (default ``0.0.0.0`` so the container is reachable) and
    ``SENTINEL_MCP_PORT`` (default ``8000``). For stdio they are ignored.

    Args:
        server: The constructed FastMCP app to run.
    """
    transport = transport_from_env()
    if transport != "stdio":
        server.settings.host = os.environ.get("SENTINEL_MCP_HOST", "0.0.0.0")
        server.settings.port = int(os.environ.get("SENTINEL_MCP_PORT", "8000"))
    server.run(transport)
