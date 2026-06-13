"""Integration tests for SimulationMCP against a REAL local Anvil sandbox.

Per CLAUDE.md §6 these are not mocked: a real Anvil node is started via fixture
and the demo contracts from prompt 2 are compiled with `forge` and deployed.
They assert the two load-bearing claims of the §12 demo:

  * the naive billing contract reverts 100% under the fee-spike scenario, and
  * adding a reentrancy guard genuinely raises the gas cost of the guarded call.

Run with: ``make check-integration`` (requires Foundry installed).
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from sentinel.mcp_servers.simulation_mcp.anvil import AnvilProcess
from sentinel.mcp_servers.simulation_mcp.config import SimulationConfig
from sentinel.mcp_servers.simulation_mcp.engine import SimulationEngine

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SANDBOX = _REPO_ROOT / "sandbox"
# Dedicated port so we never collide with a `make build-sandbox` node on 8545.
_TEST_PORT = 8546


@pytest.fixture(scope="module")
def engine() -> Iterator[SimulationEngine]:
    """Compile the sandbox contracts, boot a fresh Anvil, yield an engine."""
    if shutil.which("anvil") is None or shutil.which("forge") is None:
        pytest.skip("Foundry (anvil/forge) not installed")
    subprocess.run(["forge", "build"], cwd=_SANDBOX, check=True)
    anvil = AnvilProcess(host="127.0.0.1", port=_TEST_PORT)
    anvil.start()
    try:
        yield SimulationEngine(
            SimulationConfig(
                host="127.0.0.1", port=_TEST_PORT, artifacts_dir=_SANDBOX / "out"
            )
        )
    finally:
        anvil.stop()


def test_deploy_returns_address_and_trace(engine: SimulationEngine) -> None:
    merchant = engine.accounts[9]
    result = engine.deploy_to_fork("SubscriptionBilling", [merchant])
    assert result.address.startswith("0x")
    assert result.trace_id
    # The trace must be replayable (Golden Rule #1 provenance).
    assert engine.get_trace(result.trace_id) is not None


def test_fee_spike_yields_100_percent_revert(engine: SimulationEngine) -> None:
    merchant = engine.accounts[9]
    billing = engine.deploy_to_fork("SubscriptionBilling", [merchant])
    result = engine.get_revert_rate(billing.address, "fee_spike", n=5)
    assert result.revert_rate == 1.0
    assert result.reverts == result.n == 5
    # The revert reason really is the planted insufficient-balance DoS.
    assert any("insufficient balance" in e for e in result.sample_errors)


def test_nominal_billing_has_no_reverts(engine: SimulationEngine) -> None:
    merchant = engine.accounts[9]
    billing = engine.deploy_to_fork("SubscriptionBilling", [merchant])
    result = engine.get_revert_rate(billing.address, "nominal", n=3)
    assert result.revert_rate == 0.0


def test_reentrancy_guard_raises_gas(engine: SimulationEngine) -> None:
    merchant = engine.accounts[9]
    account = engine.accounts[1]
    fee = 10**17
    prepaid = 10**18

    naive = engine.deploy_to_fork("SubscriptionBilling", [merchant])
    guarded = engine.deploy_to_fork("SubscriptionBillingGuarded", [merchant])

    # Identical setup state on both contracts.
    engine.measure_gas(naive.address, "subscribe", [fee], value=prepaid, sender=account)
    engine.measure_gas(
        guarded.address, "subscribe", [fee], value=prepaid, sender=account
    )

    naive_cancel = engine.measure_gas(
        naive.address, "cancelSubscription", [], sender=account
    )
    guarded_cancel = engine.measure_gas(
        guarded.address, "cancelSubscription", [], sender=account
    )

    assert not naive_cancel.reverted
    assert not guarded_cancel.reverted
    # The reentrancy guard's lock SSTOREs cost real, measurable gas.
    assert guarded_cancel.gas_used > naive_cancel.gas_used


def test_reset_fork_succeeds(engine: SimulationEngine) -> None:
    result = engine.reset_fork()
    assert result.ok
    assert result.trace_id
