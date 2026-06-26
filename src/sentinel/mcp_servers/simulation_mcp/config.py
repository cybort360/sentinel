"""Configuration and the Rule 3 safety boundary for SimulationMCP.

CLAUDE.md Golden Rule #3: SimulationMCP may only ever broadcast transactions to
a LOCAL Anvil node. `assert_local_rpc` is the startup assertion that enforces
this — any non-local RPC URL is rejected before a connection is opened. The
optional mainnet fork URL (`ANVIL_FORK_URL`) is passed to Anvil read-only and is
never used as the broadcast target, so it is intentionally not checked here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

#: Hostnames that count as "local" for the broadcast RPC (Rule 3).
LOCAL_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})

#: Repo root, derived from this file's location (…/src/sentinel/mcp_servers/
#: simulation_mcp/config.py -> parents[4]).
_REPO_ROOT = Path(__file__).resolve().parents[4]


class NonLocalRPCError(ValueError):
    """Raised when SimulationMCP is pointed at a non-local broadcast RPC (Rule 3)."""


class SimulationDegradedError(RuntimeError):
    """Raised when a SimulationMCP infrastructure dependency is unavailable.

    Distinct from an on-chain revert (which is normal, expected data): this
    signals a failure to even run the simulation (no Anvil, missing artifact),
    and is always paired with a structured ``[DEGRADED]`` log entry (Rule 4).
    """

    def __init__(self, message: str, *, trace_id: str | None = None) -> None:
        """Store the degraded evidence trace id, when one was recorded."""
        super().__init__(message)
        self.trace_id = trace_id


def assert_local_rpc(rpc_url: str, *, allowlist: frozenset[str] = LOCAL_HOSTS) -> None:
    """Reject any broadcast RPC URL that is not local (CLAUDE.md Golden Rule #3).

    Args:
        rpc_url: The RPC endpoint SimulationMCP would send transactions to.
        allowlist: Hostnames permitted as broadcast targets. Defaults to
            loopback only.

    Raises:
        NonLocalRPCError: If the URL's host is missing or not in ``allowlist``.
    """
    host = urlparse(rpc_url).hostname
    if host is None or host not in allowlist:
        raise NonLocalRPCError(
            f"refusing to use non-local RPC {rpc_url!r}: SimulationMCP may only "
            f"broadcast to {sorted(allowlist)} (CLAUDE.md Golden Rule #3)"
        )


@dataclass(frozen=True)
class SimulationConfig:
    """Connection + artifact settings for a SimulationMCP engine.

    Attributes:
        host: Anvil bind host (must stay local — Rule 3).
        port: Anvil port.
        fork_url: Optional read-only mainnet RPC for Anvil to fork from.
        artifacts_dir: Foundry build output directory (``sandbox/out``) holding
            compiled contract ABIs and bytecode.
    """

    host: str = "127.0.0.1"
    port: int = 8545
    fork_url: str | None = None
    artifacts_dir: Path = field(default_factory=lambda: _REPO_ROOT / "sandbox" / "out")

    @property
    def rpc_url(self) -> str:
        """Return the local Anvil RPC URL for this config."""
        return f"http://{self.host}:{self.port}"

    @classmethod
    def from_env(cls) -> SimulationConfig:
        """Build a config from environment variables (see ``.env.example``)."""
        return cls(
            host=os.environ.get("ANVIL_HOST", "127.0.0.1"),
            port=int(os.environ.get("ANVIL_PORT", "8545")),
            fork_url=os.environ.get("ANVIL_FORK_URL") or None,
        )
