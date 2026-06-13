"""Transaction load scenarios for SimulationMCP (architecture.md §5.2, §12).

A scenario knows how to (1) set up on-chain state on a freshly deployed contract
and (2) drive one transaction of "load" against it, reporting whether that
transaction reverted. ``get_revert_rate`` / ``run_tx_spike`` repeat the action
and aggregate.

Scenarios assume a freshly deployed (or reset) contract; setup is idempotent
where it cheaply can be, but is not designed to be stacked on dirty state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentinel.mcp_servers.simulation_mcp.engine import SimulationEngine

# Prepaid/fee amounts chosen so the planted bugs are exercised deterministically.
_UNDERFUNDED_FEE = 10**18  # 1 ETH fee, with 0 prepaid -> always insufficient
_HEALTHY_FEE = 10**17  # 0.1 ETH fee
_HEALTHY_PREPAID = 10**18  # 1 ETH prepaid -> covers many cycles


class Scenario:
    """Base class for a load scenario. Subclasses override setup/act."""

    name: str = "base"
    default_n: int = 1

    def setup(self, engine: SimulationEngine, address: str) -> None:
        """Establish the on-chain precondition for this scenario."""
        raise NotImplementedError

    def act(self, engine: SimulationEngine, address: str, i: int) -> tuple[bool, str]:
        """Run one load transaction.

        Args:
            engine: The engine driving the simulation.
            address: The target contract address.
            i: The 0-based iteration index.

        Returns:
            ``(reverted, error)`` — whether the tx reverted and, if so, a short
            error description (empty string when it succeeded).
        """
        raise NotImplementedError


class _FeeSpikeScenario(Scenario):
    """Subscribers whose prepaid balances are blown out by a fee spike.

    Every settlement reverts because the atomic ``processBilling`` loop aborts on
    the first underfunded subscriber — the planted 100%-revert DoS
    (architecture.md §12, SubscriptionBilling).
    """

    name = "fee_spike"
    default_n = 5

    def setup(self, engine: SimulationEngine, address: str) -> None:
        contract = engine.contract_at(address)
        for account in engine.accounts[1:4]:
            if bool(contract.functions.subs(account).call()[2]):
                continue  # already subscribed (idempotent)
            engine.send_tx(
                contract.functions.subscribe(_UNDERFUNDED_FEE), account, value=0
            )

    def act(self, engine: SimulationEngine, address: str, i: int) -> tuple[bool, str]:
        return _attempt_billing(engine, address)


class _NominalBillingScenario(Scenario):
    """Healthy, fully-funded subscribers — settlement should always succeed."""

    name = "nominal"
    default_n = 3

    def setup(self, engine: SimulationEngine, address: str) -> None:
        contract = engine.contract_at(address)
        for account in engine.accounts[1:4]:
            if bool(contract.functions.subs(account).call()[2]):
                continue
            engine.send_tx(
                contract.functions.subscribe(_HEALTHY_FEE),
                account,
                value=_HEALTHY_PREPAID,
            )

    def act(self, engine: SimulationEngine, address: str, i: int) -> tuple[bool, str]:
        return _attempt_billing(engine, address)


def _attempt_billing(engine: SimulationEngine, address: str) -> tuple[bool, str]:
    """Send one ``processBilling`` tx; report revert status and reason."""
    contract = engine.contract_at(address)
    fn = contract.functions.processBilling()
    receipt = engine.send_tx(fn, engine.accounts[0], value=0)
    reverted = int(receipt["status"]) == 0
    error = ""
    if reverted:
        try:
            # A static call surfaces the revert reason that the receipt omits.
            fn.call({"from": engine.accounts[0]})
        except Exception as exc:  # noqa: BLE001 — any revert reason is useful here
            error = f"{type(exc).__name__}: {exc}"
    return reverted, error


_SCENARIOS: dict[str, Scenario] = {
    "fee_spike": _FeeSpikeScenario(),
    "high_congestion": _FeeSpikeScenario(),
    "nominal": _NominalBillingScenario(),
}


def get_scenario(name: str) -> Scenario:
    """Look up a scenario by name.

    Args:
        name: Registered scenario name (e.g. ``"fee_spike"``).

    Returns:
        The matching :class:`Scenario`.

    Raises:
        KeyError: If no scenario with that name is registered.
    """
    if name not in _SCENARIOS:
        raise KeyError(f"unknown scenario {name!r}; known: {sorted(_SCENARIOS)}")
    return _SCENARIOS[name]
