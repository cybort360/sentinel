"""Rule 3 enforcement: SimulationMCP must reject any non-local broadcast RPC.

These are unit tests (no Anvil needed) so they run in ``make check``. They would
fail if the startup assertion in ``config.assert_local_rpc`` / the engine ctor
were removed — that is the point.
"""

from __future__ import annotations

import pytest

from sentinel.mcp_servers.simulation_mcp.config import (
    NonLocalRPCError,
    SimulationConfig,
    assert_local_rpc,
)
from sentinel.mcp_servers.simulation_mcp.engine import SimulationEngine


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8545", "http://localhost:8545", "http://[::1]:8545"],
)
def test_local_rpc_is_allowed(url: str) -> None:
    assert_local_rpc(url)  # must not raise


@pytest.mark.parametrize(
    "url",
    [
        "https://eth-mainnet.g.alchemy.com/v2/key",
        "http://mainnet.infura.io/v3/key",
        "http://10.0.0.5:8545",
        "http://[2001:4860:4860::8888]:8545",
        "not-a-url",
    ],
)
def test_non_local_rpc_is_rejected(url: str) -> None:
    with pytest.raises(NonLocalRPCError):
        assert_local_rpc(url)


def test_engine_init_rejects_non_local_before_connecting() -> None:
    # A non-local host must be rejected at construction (Rule 3 startup
    # assertion), before any network connection is attempted.
    with pytest.raises(NonLocalRPCError):
        SimulationEngine(SimulationConfig(host="8.8.8.8", port=8545))
