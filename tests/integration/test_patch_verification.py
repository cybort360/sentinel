"""Integration test for patch verification against a REAL Anvil sandbox.

The autopilot "proven fix" claim (architecture.md §5.2): SimulationMCP runs the
same reentrancy exploit against the vulnerable ``SubscriptionBilling`` and the
patched ``SubscriptionBillingGuarded`` and shows the attack *lands* on the former
and is *blocked* on the latter — a real before/after, not a suggestion.

Run with: ``make check-integration`` (requires Foundry installed).
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.integration.sandbox_build import build_demo_contracts

from sentinel.mcp_servers.simulation_mcp.anvil import AnvilProcess
from sentinel.mcp_servers.simulation_mcp.config import SimulationConfig
from sentinel.mcp_servers.simulation_mcp.engine import SimulationEngine

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SANDBOX = _REPO_ROOT / "sandbox"
# Dedicated port (build-sandbox=8545, sim=8546, golden=8547, mcp-client=8548).
_TEST_PORT = 8549


@pytest.fixture(scope="module")
def engine() -> Iterator[SimulationEngine]:
    """Compile the sandbox, boot a fresh Anvil, yield an engine."""
    if shutil.which("anvil") is None or shutil.which("forge") is None:
        pytest.skip("Foundry (anvil/forge) not installed")
    build_demo_contracts(_SANDBOX)
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


def test_reentrancy_exploit_lands_on_the_vulnerable_contract(
    engine: SimulationEngine,
) -> None:
    result = engine.run_exploit("SubscriptionBilling", "reentrancy_drain")
    assert result.exploited
    assert not result.reverted
    assert result.drained_wei > 0  # pulled funds beyond its own stake
    assert result.trace_id


def test_reentrancy_exploit_is_blocked_by_the_guard(
    engine: SimulationEngine,
) -> None:
    result = engine.run_exploit("SubscriptionBillingGuarded", "reentrancy_drain")
    assert not result.exploited
    assert result.reverted  # the re-entrant call hits the guard and reverts
    assert result.drained_wei == 0


def test_verify_patch_confirms_the_guard_closes_the_hole(
    engine: SimulationEngine,
) -> None:
    verification = engine.verify_patch(
        "SubscriptionBilling", "SubscriptionBillingGuarded", "reentrancy_drain"
    )
    assert verification.before.exploited  # exploit lands on the vulnerable one
    assert not verification.after.exploited  # blocked on the patched one
    assert verification.fix_verified
    assert verification.trace_id
