"""FastMCP server exposing the five SimulationMCP tools (architecture.md §5.2).

This is thin wiring over :class:`SimulationEngine`; the logic and its tests live
there. Tools are registered with :meth:`FastMCP.add_tool` (rather than the
decorator) so the underlying functions keep their type annotations under
``mypy --strict``. The docstrings here are the tool descriptions the model sees.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from sentinel.mcp_servers.runtime import run_server
from sentinel.mcp_servers.simulation_mcp.config import SimulationConfig
from sentinel.mcp_servers.simulation_mcp.engine import SimulationEngine

_engine: SimulationEngine | None = None


def _get_engine() -> SimulationEngine:
    """Return the lazily-initialised engine bound to the local Anvil node."""
    global _engine
    if _engine is None:
        _engine = SimulationEngine(SimulationConfig.from_env())
    return _engine


def deploy_to_fork(contract: str, constructor_args: list[str | int]) -> dict[str, Any]:
    """Deploy a compiled contract to the local Anvil fork and return its address.

    Args:
        contract: Contract identifier, e.g. "SubscriptionBilling".
        constructor_args: Positional constructor arguments.

    Returns:
        A dict with the deployed `address`, `contract`, and a `trace_id`.
    """
    return _get_engine().deploy_to_fork(contract, list(constructor_args)).model_dump()


def measure_gas(
    address: str,
    function: str,
    args: list[str | int],
    value: int = 0,
    sender: str | None = None,
) -> dict[str, Any]:
    """Measure the actual gas a single function call consumes on the fork.

    Args:
        address: Target contract address (must have been deployed via this tool).
        function: Function name to call.
        args: Positional arguments for the function.
        value: Wei to attach (for payable functions).
        sender: Optional sender account; defaults to the deployer.

    Returns:
        A dict with `gas_used`, `reverted`, and a `trace_id`.
    """
    return (
        _get_engine()
        .measure_gas(address, function, list(args), value, sender)
        .model_dump()
    )


def run_tx_spike(address: str, scenario: str) -> dict[str, Any]:
    """Run a scripted transaction load scenario (e.g. "fee_spike") on the fork.

    Args:
        address: Target contract address.
        scenario: Scenario name, e.g. "fee_spike" or "nominal".

    Returns:
        A dict with `txs_sent`, `reverts`, `revert_rate`, and a `trace_id`.
    """
    return _get_engine().run_tx_spike(address, scenario).model_dump()


def get_revert_rate(address: str, scenario: str, n: int = 10) -> dict[str, Any]:
    """Run `n` transactions under a scenario and return the observed revert rate.

    Args:
        address: Target contract address.
        scenario: Scenario name, e.g. "fee_spike" or "nominal".
        n: Number of transactions to run.

    Returns:
        A dict with `reverts`, `revert_rate`, sample errors, and a `trace_id`.
    """
    return _get_engine().get_revert_rate(address, scenario, n).model_dump()


def reset_fork() -> dict[str, Any]:
    """Reset the Anvil fork to a clean state between rounds.

    Returns:
        A dict with `ok` and a `trace_id`.
    """
    return _get_engine().reset_fork().model_dump()


def run_exploit(target_contract: str, exploit: str) -> dict[str, Any]:
    """Run a known exploit against a contract and report whether it landed.

    Args:
        target_contract: Contract to attack, e.g. "SubscriptionBilling".
        exploit: Registered exploit name, e.g. "reentrancy_drain".

    Returns:
        A dict with `exploited`, `reverted`, `drained_wei`, and a `trace_id`.
    """
    return _get_engine().run_exploit(target_contract, exploit).model_dump()


def verify_patch(vulnerable: str, fixed: str, exploit: str) -> dict[str, Any]:
    """Prove a patch closes a hole: run the same exploit before and after.

    Runs `exploit` against the `vulnerable` contract and the `fixed` one. The
    fix is verified only if the attack lands on the former and is blocked on the
    latter — a real before/after, not a claim.

    Args:
        vulnerable: The unpatched contract (exploit should succeed here).
        fixed: The patched contract (exploit should be blocked here).
        exploit: Registered exploit name, e.g. "reentrancy_drain".

    Returns:
        A dict with `before`, `after`, `fix_verified`, and a `trace_id`.
    """
    return _get_engine().verify_patch(vulnerable, fixed, exploit).model_dump()


def build_server() -> FastMCP:
    """Construct the FastMCP app with all five SimulationMCP tools registered."""
    mcp = FastMCP("sentinel-simulation")
    tools = (
        deploy_to_fork,
        measure_gas,
        run_tx_spike,
        get_revert_rate,
        reset_fork,
        run_exploit,
        verify_patch,
    )
    for fn in tools:
        mcp.add_tool(fn, description=fn.__doc__)
    return mcp


def main() -> None:
    """Run the SimulationMCP server over the env-selected transport (default stdio)."""
    run_server(build_server())


if __name__ == "__main__":
    main()
