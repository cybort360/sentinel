"""Synchronous MCP *client* for SimulationMCP (architecture.md §5.2, §15).

This is the piece that makes the "custom MCP integration" claim end-to-end real:
instead of calling :class:`SimulationEngine` in-process, the orchestrator can
drive the **running FastMCP server over a real MCP transport**. The client speaks
the same method surface the engine does (``deploy_to_fork``/``measure_gas``/…) and
reconstructs the typed §5.2 results from each tool response, so it is a drop-in
for the engine wherever the War Room needs simulation.

Transport: stdio. The client launches ``python -m
sentinel.mcp_servers.simulation_mcp.server`` as a subprocess and exchanges MCP
JSON-RPC over its stdin/stdout — the canonical local MCP pattern, no network or
ports required. The MCP SDK is async; the War Room calls simulation
synchronously (inside the graph's executor), so a single background event-loop
thread owns the session and a request queue bridges sync callers to it. Keeping
the whole session lifecycle inside one task avoids anyio cancel-scope issues.

Rule 3 still holds: the spawned server enforces ``assert_local_rpc`` itself; this
client only chooses the transport, never the broadcast RPC.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import sys
import threading
from types import TracebackType
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from sentinel.mcp_servers.simulation_mcp.results import (
    DeployResult,
    GasResult,
    ResetResult,
    RevertRateResult,
    TxSpikeResult,
)

_DEFAULT_ARGS = ("-m", "sentinel.mcp_servers.simulation_mcp.server")


class SimulationMCPClientError(RuntimeError):
    """Raised when a SimulationMCP tool call fails over the transport."""


def _unwrap(result: Any) -> dict[str, Any]:
    """Extract a tool's result dict from a ``CallToolResult`` (any SDK shaping).

    Args:
        result: The raw ``CallToolResult`` returned by ``session.call_tool``.

    Returns:
        The tool's structured payload as a dict.

    Raises:
        SimulationMCPClientError: If the tool errored or returned no payload.
    """
    if getattr(result, "isError", False):
        text = ""
        content = getattr(result, "content", None) or []
        if content and hasattr(content[0], "text"):
            text = content[0].text
        raise SimulationMCPClientError(text or "SimulationMCP tool returned an error")

    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        if "trace_id" in structured:
            return structured
        # FastMCP may wrap a bare return under a single key (e.g. "result").
        if len(structured) == 1:
            inner = next(iter(structured.values()))
            if isinstance(inner, dict):
                return inner
        return structured

    content = getattr(result, "content", None) or []
    if content and hasattr(content[0], "text"):
        parsed = json.loads(content[0].text)
        if isinstance(parsed, dict):
            return parsed
    raise SimulationMCPClientError("SimulationMCP tool returned no structured content")


class SimulationMCPClient:
    """Drop-in for :class:`SimulationEngine`, backed by the MCP server over stdio.

    Use as a context manager so the subprocess and event-loop thread are always
    torn down::

        with SimulationMCPClient() as sim:
            dep = sim.deploy_to_fork("SubscriptionBilling", [merchant])
            gas = sim.measure_gas(dep.address, "subscribe", [fee], value=prepaid)
    """

    def __init__(
        self,
        *,
        command: str | None = None,
        args: tuple[str, ...] = _DEFAULT_ARGS,
        env: dict[str, str] | None = None,
        startup_timeout: float = 30.0,
        call_timeout: float = 60.0,
    ) -> None:
        """Spawn the server and connect over stdio.

        Args:
            command: Interpreter to launch the server with (default: this venv's
                ``sys.executable``).
            args: Module-launch args (default: run the SimulationMCP server).
            env: Environment for the server process; defaults to the current
                environment so it inherits ``ANVIL_HOST``/``ANVIL_PORT`` etc.
            startup_timeout: Seconds to wait for the MCP session to initialise.
            call_timeout: Per-tool-call timeout in seconds.
        """
        self._params = StdioServerParameters(
            command=command or sys.executable,
            args=list(args),
            env=env if env is not None else dict(os.environ),
        )
        self._call_timeout = call_timeout
        self._queue: asyncio.Queue[_Job | None] | None = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="sim-mcp-client", daemon=True
        )
        self._thread.start()
        ready: concurrent.futures.Future[None] = concurrent.futures.Future()
        asyncio.run_coroutine_threadsafe(self._runner(ready), self._loop)
        ready.result(timeout=startup_timeout)

    # -- session lifecycle (all on the background loop, in one task) -------- #

    async def _runner(self, ready: concurrent.futures.Future[None]) -> None:
        """Own the MCP session and serve queued tool calls until shutdown."""
        self._queue = asyncio.Queue()
        try:
            async with (
                stdio_client(self._params) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                ready.set_result(None)
                while True:
                    job = await self._queue.get()
                    if job is None:
                        break
                    await self._serve(session, job)
        except Exception as exc:  # noqa: BLE001 — surface startup failure to caller
            if not ready.done():
                ready.set_exception(exc)

    @staticmethod
    async def _serve(session: ClientSession, job: _Job) -> None:
        """Execute one queued tool call and resolve its future."""
        name, arguments, fut = job
        try:
            raw = await session.call_tool(name, arguments)
            fut.set_result(_unwrap(raw))
        except Exception as exc:  # noqa: BLE001 — propagate to the sync caller
            fut.set_exception(exc)

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Submit a tool call to the session thread and block for its result."""
        if self._queue is None:
            raise SimulationMCPClientError("client is not connected")
        fut: concurrent.futures.Future[dict[str, Any]] = concurrent.futures.Future()
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (name, arguments, fut))
        return fut.result(timeout=self._call_timeout)

    # -- the engine-compatible tool surface --------------------------------- #

    def deploy_to_fork(
        self, contract: str, constructor_args: list[str | int]
    ) -> DeployResult:
        """Deploy a contract to the fork via the MCP server, returning its result."""
        return DeployResult(
            **self._call(
                "deploy_to_fork",
                {"contract": contract, "constructor_args": list(constructor_args)},
            )
        )

    def measure_gas(
        self,
        address: str,
        function: str,
        args: list[str | int],
        value: int = 0,
        sender: str | None = None,
    ) -> GasResult:
        """Measure a single call's gas via the MCP server."""
        return GasResult(
            **self._call(
                "measure_gas",
                {
                    "address": address,
                    "function": function,
                    "args": list(args),
                    "value": value,
                    "sender": sender,
                },
            )
        )

    def run_tx_spike(self, address: str, scenario: str) -> TxSpikeResult:
        """Run a scripted load scenario via the MCP server."""
        return TxSpikeResult(
            **self._call("run_tx_spike", {"address": address, "scenario": scenario})
        )

    def get_revert_rate(
        self, address: str, scenario: str, n: int = 10
    ) -> RevertRateResult:
        """Measure the observed revert rate over ``n`` txs via the MCP server."""
        return RevertRateResult(
            **self._call(
                "get_revert_rate",
                {"address": address, "scenario": scenario, "n": n},
            )
        )

    def reset_fork(self) -> ResetResult:
        """Reset the fork between rounds via the MCP server."""
        return ResetResult(**self._call("reset_fork", {}))

    # -- teardown ----------------------------------------------------------- #

    def close(self) -> None:
        """Shut the session, subprocess, and event-loop thread down cleanly."""
        if self._loop.is_closed():
            return
        if self._queue is not None:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        if not self._loop.is_closed():
            self._loop.close()

    def __enter__(self) -> SimulationMCPClient:
        """Return self for use as a context manager."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Tear the client down on context exit."""
        self.close()


# (name, arguments, result_future) submitted from a sync caller to the session.
_Job = tuple[str, dict[str, Any], "concurrent.futures.Future[dict[str, Any]]"]
