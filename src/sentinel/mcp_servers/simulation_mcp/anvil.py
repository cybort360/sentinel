"""Manage a local Anvil node as a subprocess (architecture.md §5.2).

SimulationMCP wraps Anvil (Foundry) over a subprocess and talks to it with
web3.py. This module owns the process lifecycle: start, wait-until-ready, stop.
The bound host is asserted local on construction (CLAUDE.md Golden Rule #3).
"""

from __future__ import annotations

import socket
import subprocess
import time
from types import TracebackType

from sentinel.mcp_servers.simulation_mcp.config import (
    SimulationDegradedError,
    assert_local_rpc,
)
from sentinel.observability.trace_logger import get_logger

_log = get_logger("simulation_mcp.anvil")


class AnvilProcess:
    """A managed local Anvil instance.

    Use as a context manager, or call :meth:`start` / :meth:`stop` directly.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8545,
        fork_url: str | None = None,
        startup_timeout: float = 30.0,
    ) -> None:
        """Initialise (does not start) a local Anvil process.

        Args:
            host: Bind host — must be local (Rule 3).
            port: Bind port.
            fork_url: Optional read-only RPC for Anvil to fork mainnet state.
            startup_timeout: Seconds to wait for the RPC port to accept
                connections before giving up.
        """
        self.host = host
        self.port = port
        self.fork_url = fork_url
        self.startup_timeout = startup_timeout
        self._proc: subprocess.Popen[bytes] | None = None
        assert_local_rpc(self.rpc_url)

    @property
    def rpc_url(self) -> str:
        """Return the RPC URL this Anvil instance listens on."""
        return f"http://{self.host}:{self.port}"

    def start(self) -> str:
        """Spawn Anvil and block until its RPC port is accepting connections.

        Returns:
            The RPC URL once the node is ready.

        Raises:
            SimulationDegradedError: If Anvil exits early or never becomes ready.
        """
        if self._proc is not None:
            return self.rpc_url

        args = ["anvil", "--host", self.host, "--port", str(self.port)]
        if self.fork_url:
            args += ["--fork-url", self.fork_url]
        _log.info("anvil_start", rpc_url=self.rpc_url, fork=bool(self.fork_url))
        self._proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT
        )

        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                self._proc = None
                _log.error(
                    "[DEGRADED] anvil exited during startup", rpc_url=self.rpc_url
                )
                raise SimulationDegradedError(
                    f"anvil exited before it was ready ({self.rpc_url})"
                )
            if self._port_open():
                _log.info("anvil_ready", rpc_url=self.rpc_url)
                return self.rpc_url
            time.sleep(0.2)

        self.stop()
        _log.error("[DEGRADED] anvil startup timed out", rpc_url=self.rpc_url)
        raise SimulationDegradedError(
            f"anvil did not become ready within {self.startup_timeout}s"
        )

    def stop(self) -> None:
        """Terminate the Anvil process if running (idempotent)."""
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=5)
        finally:
            _log.info("anvil_stop", rpc_url=self.rpc_url)
            self._proc = None

    def _port_open(self) -> bool:
        """Return True if the RPC TCP port currently accepts connections."""
        try:
            with socket.create_connection((self.host, self.port), timeout=0.5):
                return True
        except OSError:
            return False

    def __enter__(self) -> AnvilProcess:
        """Start Anvil on context entry."""
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Stop Anvil on context exit."""
        self.stop()
