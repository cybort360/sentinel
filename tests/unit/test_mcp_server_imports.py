"""Each MCP server must import cleanly in a *fresh* interpreter.

Regression guard for a circular import (``memory.schema`` ->
``orchestrator.schema`` -> orchestrator ``__init__`` -> ``checkpoint`` ->
``memory.schema``) that only surfaced when a process imported ``memory.schema``
first — e.g. ``python -m sentinel.mcp_servers.memory_mcp.server`` cold, as the
docker-compose stack does. Within a single pytest process the modules are
already loaded, so the cycle is invisible; these checks spawn a subprocess to
reproduce the real cold-start path and would fail if the eager re-exports
returned.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

_SERVERS = [
    "sentinel.mcp_servers.memory_mcp.server",
    "sentinel.mcp_servers.codebase_mcp.server",
    "sentinel.mcp_servers.simulation_mcp.server",
]


@pytest.mark.parametrize("module", _SERVERS)
def test_server_imports_cold(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {module} as s; assert s.build_server"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_memory_schema_imports_cold() -> None:
    # The exact first-import that triggered the cycle.
    result = subprocess.run(
        [sys.executable, "-c", "from sentinel.memory.schema import MemoryRecord"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
