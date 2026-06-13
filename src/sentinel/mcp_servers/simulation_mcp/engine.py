"""SimulationMCP engine: deploy + exercise contracts on a local Anvil fork.

This is the testable core behind the FastMCP server (architecture.md §5.2). It
wraps web3.py over a local Anvil node and implements the five SimulationMCP
tools. Every public tool method:

  * returns a typed result carrying a ``trace_id``, and
  * emits a structured trace through the trace logger (architecture.md §10),

so that every number an agent later cites is replayable (Golden Rule #1).
Transactions are sent from Anvil's unlocked dev accounts, so no private keys are
handled anywhere (which also keeps us comfortably inside Golden Rule #3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from web3 import Web3

from sentinel.mcp_servers.simulation_mcp.artifacts import load_artifact
from sentinel.mcp_servers.simulation_mcp.config import (
    SimulationConfig,
    SimulationDegradedError,
    assert_local_rpc,
)
from sentinel.mcp_servers.simulation_mcp.results import (
    DeployResult,
    GasResult,
    ResetResult,
    RevertRateResult,
    TxSpikeResult,
)
from sentinel.mcp_servers.simulation_mcp.scenarios import get_scenario
from sentinel.observability.trace_logger import get_logger

#: Gas cap for state-changing sends. Generous so web3 never auto-estimates (which
#: would raise on a reverting tx instead of mining it with status 0); the cap
#: does not affect the *measured* gasUsed.
_DEFAULT_SEND_GAS = 12_000_000
_DEFAULT_DEPLOY_GAS = 6_000_000
_MAX_SAMPLE_ERRORS = 3


@dataclass(frozen=True)
class _DeployedContract:
    """Bookkeeping for a contract deployed through this engine."""

    name: str
    abi: list[Any]


class SimulationEngine:
    """Drives an Anvil fork: deploy, measure gas, run scenarios, reset."""

    def __init__(self, config: SimulationConfig) -> None:
        """Connect to the configured local Anvil node.

        Args:
            config: Connection + artifact settings.

        Raises:
            NonLocalRPCError: If ``config.rpc_url`` is not local (Rule 3).
            SimulationDegradedError: If the node cannot be reached.
        """
        assert_local_rpc(config.rpc_url)  # Rule 3 startup assertion.
        self._config = config
        self._log = get_logger("simulation_mcp")
        self._w3 = Web3(Web3.HTTPProvider(config.rpc_url))
        self._registry: dict[str, _DeployedContract] = {}
        self._traces: dict[str, dict[str, Any]] = {}
        if not self._w3.is_connected():
            self._log.error(
                "[DEGRADED] cannot connect to Anvil", rpc_url=config.rpc_url
            )
            raise SimulationDegradedError(
                f"cannot connect to Anvil at {config.rpc_url}"
            )

    # -- scenario-facing helpers ----------------------------------------------

    @property
    def accounts(self) -> list[str]:
        """Anvil's unlocked dev accounts (checksummed addresses)."""
        return [str(a) for a in self._w3.eth.accounts]

    @property
    def deployer(self) -> str:
        """The default deployer/sender account (Anvil account #0)."""
        return self.accounts[0]

    def contract_at(self, address: str) -> Any:
        """Return a web3 contract bound to a previously deployed address.

        Args:
            address: A contract address deployed through this engine.

        Raises:
            SimulationDegradedError: If the address is not in the registry.
        """
        addr = Web3.to_checksum_address(address)
        info = self._registry.get(addr)
        if info is None:
            self._log.error("[DEGRADED] unknown contract address", address=addr)
            raise SimulationDegradedError(
                f"address {addr} was not deployed by this engine"
            )
        return self._w3.eth.contract(address=addr, abi=info.abi)

    def send_tx(
        self, fn: Any, sender: str, value: int = 0, gas: int = _DEFAULT_SEND_GAS
    ) -> Any:
        """Send a contract function call and return its mined receipt.

        Args:
            fn: A bound web3 contract function (``contract.functions.foo(...)``).
            sender: The ``from`` account (an unlocked Anvil account).
            value: Wei to attach.
            gas: Gas cap (kept explicit so reverts mine with status 0).

        Returns:
            The transaction receipt (web3 ``AttributeDict``).
        """
        tx_hash = fn.transact({"from": sender, "value": value, "gas": gas})
        return self._w3.eth.wait_for_transaction_receipt(tx_hash)

    # -- tools ----------------------------------------------------------------

    def deploy_to_fork(
        self, contract: str, constructor_args: list[Any]
    ) -> DeployResult:
        """Deploy a compiled contract to the local fork.

        Args:
            contract: Contract identifier (``"Name"`` or ``"File.sol:Name"``).
            constructor_args: Positional constructor arguments.

        Returns:
            A :class:`DeployResult` with the deployed address and a trace id.

        Raises:
            SimulationDegradedError: If the artifact is missing or deployment fails.
        """
        try:
            artifact = load_artifact(contract, self._config.artifacts_dir)
            factory = self._w3.eth.contract(
                abi=artifact.abi, bytecode=artifact.bytecode
            )
            tx_hash = factory.constructor(*constructor_args).transact(
                {"from": self.deployer, "gas": _DEFAULT_DEPLOY_GAS}
            )
            receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash)
        except FileNotFoundError as exc:
            self._log.error(
                "[DEGRADED] deploy_to_fork artifact missing", contract=contract
            )
            raise SimulationDegradedError(str(exc)) from exc
        address = str(Web3.to_checksum_address(receipt["contractAddress"]))
        self._registry[address] = _DeployedContract(
            name=artifact.name, abi=artifact.abi
        )
        trace_id = self._record(
            "deploy_to_fork",
            summary=f"deployed {artifact.name} at {address}",
            contract=contract,
            address=address,
        )
        return DeployResult(trace_id=trace_id, contract=contract, address=address)

    def measure_gas(
        self,
        address: str,
        function: str,
        args: list[Any],
        value: int = 0,
        sender: str | None = None,
    ) -> GasResult:
        """Measure the actual gas a function call consumes.

        Args:
            address: Target contract address (deployed via this engine).
            function: Function name to call.
            args: Positional arguments for the function.
            value: Wei to attach (for payable functions).
            sender: Sender account; defaults to the deployer.

        Returns:
            A :class:`GasResult` with the consumed gas and a trace id.
        """
        contract = self.contract_at(address)
        fn = getattr(contract.functions, function)(*args)
        receipt = self.send_tx(fn, sender or self.deployer, value=value)
        gas_used = int(receipt["gasUsed"])
        reverted = int(receipt["status"]) == 0
        trace_id = self._record(
            "measure_gas",
            summary=f"{function} used {gas_used} gas (reverted={reverted})",
            address=str(Web3.to_checksum_address(address)),
            function=function,
            gas_used=gas_used,
            reverted=reverted,
        )
        return GasResult(
            trace_id=trace_id,
            address=str(Web3.to_checksum_address(address)),
            function=function,
            gas_used=gas_used,
            reverted=reverted,
        )

    def get_revert_rate(self, address: str, scenario: str, n: int) -> RevertRateResult:
        """Run ``n`` transactions under a scenario and report the revert rate.

        Args:
            address: Target contract address.
            scenario: Registered scenario name (e.g. ``"fee_spike"``).
            n: Number of transactions to run.

        Returns:
            A :class:`RevertRateResult` with the observed revert rate and a
            trace id.
        """
        sent, reverts, errors = self._drive(address, scenario, n)
        rate = reverts / sent if sent else 0.0
        trace_id = self._record(
            "get_revert_rate",
            summary=f"{scenario}: {reverts}/{sent} reverted ({rate:.0%})",
            address=str(Web3.to_checksum_address(address)),
            scenario=scenario,
            n=sent,
            reverts=reverts,
            revert_rate=rate,
        )
        return RevertRateResult(
            trace_id=trace_id,
            address=str(Web3.to_checksum_address(address)),
            scenario=scenario,
            n=sent,
            reverts=reverts,
            revert_rate=rate,
            sample_errors=errors,
        )

    def run_tx_spike(self, address: str, scenario: str) -> TxSpikeResult:
        """Run a scripted load scenario at its default size.

        Args:
            address: Target contract address.
            scenario: Registered scenario name (e.g. ``"fee_spike"``).

        Returns:
            A :class:`TxSpikeResult` summarising the load and a trace id.
        """
        n = get_scenario(scenario).default_n
        sent, reverts, errors = self._drive(address, scenario, n)
        rate = reverts / sent if sent else 0.0
        trace_id = self._record(
            "run_tx_spike",
            summary=f"{scenario}: drove {sent} txs, {reverts} reverted ({rate:.0%})",
            address=str(Web3.to_checksum_address(address)),
            scenario=scenario,
            txs_sent=sent,
            reverts=reverts,
            revert_rate=rate,
        )
        return TxSpikeResult(
            trace_id=trace_id,
            address=str(Web3.to_checksum_address(address)),
            scenario=scenario,
            txs_sent=sent,
            reverts=reverts,
            revert_rate=rate,
            sample_errors=errors,
        )

    def reset_fork(self) -> ResetResult:
        """Reset Anvil to a clean state between rounds (architecture.md §5.2).

        Returns:
            A :class:`ResetResult`; ``ok=False`` (with an error) if the reset
            RPC failed — degraded, not fatal (Rule 4).
        """
        try:
            self._w3.provider.make_request("anvil_reset", [])
            self._registry.clear()
        except Exception as exc:  # noqa: BLE001 — degrade gracefully (Rule 4)
            self._log.warning("[DEGRADED] reset_fork failed", error=str(exc))
            trace_id = self._record("reset_fork", summary="reset failed", ok=False)
            return ResetResult(trace_id=trace_id, ok=False, error=str(exc))
        trace_id = self._record("reset_fork", summary="fork reset", ok=True)
        return ResetResult(trace_id=trace_id, ok=True)

    # -- internals ------------------------------------------------------------

    def _drive(self, address: str, scenario: str, n: int) -> tuple[int, int, list[str]]:
        """Set up a scenario then run ``n`` of its actions, aggregating reverts."""
        scen = get_scenario(scenario)
        scen.setup(self, address)
        sent = 0
        reverts = 0
        errors: list[str] = []
        for i in range(n):
            reverted, error = scen.act(self, address, i)
            sent += 1
            if reverted:
                reverts += 1
                if error and len(errors) < _MAX_SAMPLE_ERRORS:
                    errors.append(error)
        return sent, reverts, errors

    def _record(self, tool: str, *, summary: str, **fields: Any) -> str:
        """Generate a trace id, log a structured trace event, and store it."""
        trace_id = uuid4().hex
        entry: dict[str, Any] = {"tool": tool, "summary": summary, **fields}
        self._traces[trace_id] = entry
        self._log.info("sim_trace", trace_id=trace_id, **entry)
        return trace_id

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        """Return a previously recorded trace by id (for replay), or None."""
        return self._traces.get(trace_id)
