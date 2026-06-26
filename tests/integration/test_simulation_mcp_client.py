"""Integration test for the SimulationMCP *client* (architecture.md §5.2, §15).

This is the end-to-end proof that the orchestrator can consume the custom MCP
server as a real client — not just call the engine in-process. It launches the
FastMCP ``sentinel-simulation`` server over a stdio transport via
:class:`SimulationMCPClient` and drives the same load-bearing claims as the
engine integration test, but every number here crosses a genuine MCP round-trip
(client → JSON-RPC → server → Anvil → back). If the MCP wiring regresses, these
turn red.

Run with: ``make check-integration`` (requires Foundry installed).
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.integration.sandbox_build import build_demo_contracts

from sentinel.mcp_servers.simulation_mcp.anvil import AnvilProcess
from sentinel.mcp_servers.simulation_mcp.client import SimulationMCPClient
from sentinel.mcp_servers.simulation_mcp.config import SimulationConfig
from sentinel.mcp_servers.simulation_mcp.engine import SimulationEngine

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SANDBOX = _REPO_ROOT / "sandbox"
# Dedicated port: avoids build-sandbox (8545), the sim engine test (8546), and
# the golden-trace node (8547).
_TEST_PORT = 8548


@pytest.fixture(scope="module")
def sandbox() -> Iterator[tuple[SimulationMCPClient, SimulationEngine]]:
    """Boot Anvil, yield (MCP client, engine) both bound to the same node.

    The engine is used only to read Anvil's deterministic dev accounts for
    constructor args; every asserted simulation result comes through the client.
    """
    if shutil.which("anvil") is None or shutil.which("forge") is None:
        pytest.skip("Foundry (anvil/forge) not installed")
    build_demo_contracts(_SANDBOX)
    anvil = AnvilProcess(host="127.0.0.1", port=_TEST_PORT)
    anvil.start()
    env = {**os.environ, "ANVIL_HOST": "127.0.0.1", "ANVIL_PORT": str(_TEST_PORT)}
    client = SimulationMCPClient(env=env)
    engine = SimulationEngine(
        SimulationConfig(
            host="127.0.0.1", port=_TEST_PORT, artifacts_dir=_SANDBOX / "out"
        )
    )
    try:
        yield client, engine
    finally:
        client.close()
        anvil.stop()


def test_deploy_over_mcp_returns_address_and_trace(
    sandbox: tuple[SimulationMCPClient, SimulationEngine],
) -> None:
    client, engine = sandbox
    merchant = engine.accounts[9]
    result = client.deploy_to_fork("SubscriptionBilling", [merchant])
    assert result.address.startswith("0x")
    assert result.trace_id  # Golden Rule #1 provenance, end to end over MCP


def test_fee_spike_revert_rate_over_mcp(
    sandbox: tuple[SimulationMCPClient, SimulationEngine],
) -> None:
    client, engine = sandbox
    billing = client.deploy_to_fork("SubscriptionBilling", [engine.accounts[9]])
    result = client.get_revert_rate(billing.address, "fee_spike", n=5)
    assert result.revert_rate == 1.0
    assert result.reverts == result.n == 5
    assert result.trace_id


def test_measure_gas_over_mcp(
    sandbox: tuple[SimulationMCPClient, SimulationEngine],
) -> None:
    client, engine = sandbox
    merchant, account = engine.accounts[9], engine.accounts[1]
    billing = client.deploy_to_fork("SubscriptionBilling", [merchant])
    client.measure_gas(
        billing.address, "subscribe", [10**17], value=10**18, sender=account
    )
    cancel = client.measure_gas(
        billing.address, "cancelSubscription", [], sender=account
    )
    assert cancel.gas_used > 0
    assert not cancel.reverted
    assert cancel.trace_id


def test_reset_fork_over_mcp(
    sandbox: tuple[SimulationMCPClient, SimulationEngine],
) -> None:
    client, _ = sandbox
    result = client.reset_fork()
    assert result.ok
    assert result.trace_id


def test_verify_patch_over_mcp(
    sandbox: tuple[SimulationMCPClient, SimulationEngine],
) -> None:
    client, _ = sandbox
    v = client.verify_patch(
        "SubscriptionBilling", "SubscriptionBillingGuarded", "reentrancy_drain"
    )
    assert v.before.exploited and not v.after.exploited
    assert v.fix_verified
    assert v.trace_id
