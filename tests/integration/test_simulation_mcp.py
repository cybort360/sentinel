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
from tests.integration.sandbox_build import build_demo_contracts

from sentinel.mcp_servers.simulation_mcp.anvil import AnvilProcess
from sentinel.mcp_servers.simulation_mcp.config import (
    SimulationConfig,
    SimulationDegradedError,
)
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


def test_deploy_returns_address_and_trace(engine: SimulationEngine) -> None:
    merchant = engine.accounts[9]
    result = engine.deploy_to_fork("SubscriptionBilling", [merchant])
    assert result.address.startswith("0x")
    assert result.trace_id
    # The trace must be replayable (Golden Rule #1 provenance).
    assert engine.get_trace(result.trace_id) is not None


def test_missing_artifact_degradation_has_trace(engine: SimulationEngine) -> None:
    with pytest.raises(SimulationDegradedError) as exc_info:
        engine.deploy_to_fork("contracts/staged_patches/missing.sol", [])

    trace_id = exc_info.value.trace_id
    assert trace_id
    trace = engine.get_trace(trace_id)
    assert trace is not None
    assert trace["tool"] == "deploy_to_fork"
    assert trace["degraded"] is True
    assert "patch_artifact_missing" in trace["summary"]


def test_uploaded_three_address_constructor_uses_inferred_args(
    engine: SimulationEngine,
) -> None:
    source = _write_upload_contract(
        "ThreeAddressConstructorUpload",
        """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract ThreeAddressConstructorUpload {
    address public a;
    address public b;
    address public c;

    constructor(address _a, address _b, address _c) {
        a = _a;
        b = _b;
        c = _c;
    }
}
""",
    )
    _forge_build_upload(source)

    deployed = engine.deploy_to_fork("ThreeAddressConstructorUpload", [])
    trace = engine.get_trace(deployed.trace_id)

    assert trace is not None
    args = trace["constructor_args"]
    assert len(args) == 3
    assert all(str(arg).startswith("0x") for arg in args)
    assert all(int(str(arg), 16) != 0 for arg in args)
    assert any(
        t.get("tool") == "infer_constructor_args"
        and t.get("contract") == "ThreeAddressConstructorUpload"
        for t in engine._traces.values()
    )


def test_unsupported_constructor_args_mark_dynamic_verification_unavailable(
    engine: SimulationEngine,
) -> None:
    source = _write_upload_contract(
        "UnsupportedArrayConstructorUpload",
        """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract UnsupportedArrayConstructorUpload {
    address[] public owners;

    constructor(address[] memory _owners) {
        owners = _owners;
    }
}
""",
    )
    _forge_build_upload(source)

    with pytest.raises(SimulationDegradedError) as exc_info:
        engine.deploy_to_fork("UnsupportedArrayConstructorUpload", [])

    assert "dynamic_verification_unavailable" in str(exc_info.value)
    trace = engine.get_trace(exc_info.value.trace_id or "")
    assert trace is not None
    assert trace["tool"] == "infer_constructor_args"
    assert trace["degraded"] is True
    assert trace["argument_type"] == "address[]"


def test_compiled_staged_uploaded_patch_can_be_deployed(
    engine: SimulationEngine,
) -> None:
    source = _write_staged_patch_contract(
        "patchdeployable",
        "StagedPatchedUpload",
        """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract StagedPatchedUpload {
    address public owner;

    constructor(address _owner) {
        owner = _owner;
    }
}
""",
    )
    _forge_build_upload(source)

    deployed = engine.deploy_to_fork(
        "contracts/staged_patches/patchdeployable.sol",
        [],
    )

    trace = engine.get_trace(deployed.trace_id)
    assert deployed.address.startswith("0x")
    assert trace is not None
    assert trace["contract"] == "contracts/staged_patches/patchdeployable.sol"
    assert "deployed StagedPatchedUpload" in trace["summary"]


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


def _write_upload_contract(name: str, source: str) -> Path:
    path = _SANDBOX / "contracts" / "audit_workdir" / f"{name}.sol"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    return path


def _write_staged_patch_contract(patch_id: str, name: str, source: str) -> Path:
    path = _SANDBOX / "contracts" / "staged_patches" / f"{patch_id}.sol"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    return path


def _forge_build_upload(path: Path) -> None:
    subprocess.run(
        ["forge", "build", path.relative_to(_SANDBOX).as_posix()],
        cwd=_SANDBOX,
        check=True,
        capture_output=True,
        text=True,
    )
