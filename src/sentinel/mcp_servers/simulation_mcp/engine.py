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
from sentinel.mcp_servers.simulation_mcp.exploits import get_exploit
from sentinel.mcp_servers.simulation_mcp.results import (
    DeployResult,
    ExploitResult,
    GasResult,
    PatchVerification,
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
            if not constructor_args:
                constructor_args = self._infer_constructor_args(contract, artifact.abi)
            factory = self._w3.eth.contract(
                abi=artifact.abi, bytecode=artifact.bytecode
            )
            tx_hash = factory.constructor(*constructor_args).transact(
                {"from": self.deployer, "gas": _DEFAULT_DEPLOY_GAS}
            )
            receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash)
        except FileNotFoundError as exc:
            summary = (
                "patch_artifact_missing: missing artifact for staged patch"
                if "staged_patches" in contract
                else "deploy failed: missing artifact"
            )
            raise self._degraded_error(
                "deploy_to_fork",
                summary,
                exc,
                contract=contract,
            ) from exc
        except SimulationDegradedError:
            raise
        except Exception as exc:
            raise self._degraded_error(
                "deploy_to_fork",
                "deploy failed",
                exc,
                contract=contract,
            ) from exc
        address = str(Web3.to_checksum_address(receipt["contractAddress"]))
        self._registry[address] = _DeployedContract(
            name=artifact.name, abi=artifact.abi
        )
        trace_id = self._record(
            "deploy_to_fork",
            summary=f"deployed {artifact.name} at {address}",
            contract=contract,
            address=address,
            constructor_args=constructor_args,
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
        try:
            contract = self.contract_at(address)
            fn = getattr(contract.functions, function)(*args)
            receipt = self.send_tx(fn, sender or self.deployer, value=value)
        except Exception as exc:
            raise self._degraded_error(
                "measure_gas",
                "gas measurement failed",
                exc,
                address=address,
                function=function,
                args=args,
                value=value,
                sender=sender,
            ) from exc
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

    def run_exploit(self, target_contract: str, exploit: str) -> ExploitResult:
        """Run a known exploit against a contract and report whether it landed.

        Args:
            target_contract: The contract to attack (e.g. ``"SubscriptionBilling"``).
            exploit: Registered exploit name (e.g. ``"reentrancy_drain"``).

        Returns:
            An :class:`ExploitResult` — ``exploited`` is the verdict, with the
            wei drained and whether the attack reverted, plus a trace id.

        Raises:
            SimulationDegradedError: If the exploit cannot run (missing artifact,
                unknown exploit).
        """
        try:
            exploited, drained, reverted = get_exploit(exploit)(self, target_contract)
        except (KeyError, SimulationDegradedError) as exc:
            existing_trace_id = getattr(exc, "trace_id", None)
            if existing_trace_id:
                raise
            raise self._degraded_error(
                "run_exploit",
                "exploit run failed",
                exc,
                contract=target_contract,
                exploit=exploit,
            ) from exc
        verdict = (
            "exploited" if exploited else ("reverted" if reverted else "no effect")
        )
        summary = f"{exploit} vs {target_contract}: {verdict} (drained {drained} wei)"
        trace_id = self._record(
            "run_exploit",
            summary=summary,
            exploit=exploit,
            contract=target_contract,
            exploited=exploited,
            reverted=reverted,
            drained_wei=drained,
        )
        return ExploitResult(
            trace_id=trace_id,
            exploit=exploit,
            contract=target_contract,
            exploited=exploited,
            reverted=reverted,
            drained_wei=drained,
        )

    def verify_patch(
        self, vulnerable: str, fixed: str, exploit: str
    ) -> PatchVerification:
        """Prove a patch closes a hole: run the same exploit before and after.

        Runs ``exploit`` against the ``vulnerable`` contract and the ``fixed``
        one. The fix is verified only if the attack lands on the former and is
        blocked on the latter — a real before/after, not a claim.

        Args:
            vulnerable: The unpatched contract (the exploit should succeed here).
            fixed: The patched contract (the exploit should be blocked here).
            exploit: Registered exploit name.

        Returns:
            A :class:`PatchVerification` carrying both runs and ``fix_verified``.
        """
        before = self.run_exploit(vulnerable, exploit)
        self.reset_fork()
        after = self.run_exploit(fixed, exploit)
        fix_verified = before.exploited and not after.exploited
        before_s = "exploited" if before.exploited else "safe"
        after_s = "blocked" if not after.exploited else "still exploited"
        verdict_s = "FIX VERIFIED" if fix_verified else "NOT VERIFIED"
        summary = (
            f"{exploit}: {vulnerable} {before_s} -> {fixed} {after_s} ({verdict_s})"
        )
        trace_id = self._record(
            "verify_patch",
            summary=summary,
            exploit=exploit,
            vulnerable_contract=vulnerable,
            fixed_contract=fixed,
            fix_verified=fix_verified,
        )
        return PatchVerification(
            trace_id=trace_id,
            exploit=exploit,
            vulnerable_contract=vulnerable,
            fixed_contract=fixed,
            before=before,
            after=after,
            fix_verified=fix_verified,
        )

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

    def _infer_constructor_args(self, contract: str, abi: list[Any]) -> list[Any]:
        """Infer safe generic constructor args for ABI-supported scalar types."""
        ctor = next(
            (entry for entry in abi if entry.get("type") == "constructor"),
            None,
        )
        inputs = list(ctor.get("inputs", [])) if ctor else []
        if not inputs:
            return []
        args: list[Any] = []
        address_i = 1
        for item in inputs:
            typ = str(item.get("type", ""))
            name = str(item.get("name") or f"arg{len(args)}")
            if "[" in typ or typ.startswith(("tuple", "struct")):
                self._raise_constructor_unavailable(
                    contract, typ, name, args, "arrays and structs are unsupported"
                )
            if typ == "address":
                account = self.accounts[address_i % len(self.accounts)]
                address_i += 1
                if int(account, 16) == 0:
                    self._raise_constructor_unavailable(
                        contract, typ, name, args, "zero address would be unsafe"
                    )
                args.append(account)
                continue
            if typ.startswith(("uint", "int")):
                args.append(1)
                continue
            if typ == "bool":
                args.append(False)
                continue
            if typ == "string":
                args.append("test")
                continue
            if typ == "bytes":
                args.append("0x")
                continue
            if typ.startswith("bytes"):
                try:
                    size = int(typ.removeprefix("bytes"))
                except ValueError:
                    self._raise_constructor_unavailable(
                        contract, typ, name, args, "unsupported bytes type"
                    )
                args.append("0x" + ("00" * size))
                continue
            self._raise_constructor_unavailable(
                contract, typ, name, args, "unsupported constructor type"
            )
        self._record(
            "infer_constructor_args",
            summary=f"inferred constructor args for {contract}",
            contract=contract,
            constructor_arg_types=[str(i.get("type", "")) for i in inputs],
            constructor_args=args,
        )
        return args

    def _raise_constructor_unavailable(
        self,
        contract: str,
        typ: str,
        name: str,
        partial_args: list[Any],
        reason: str,
    ) -> None:
        """Record and raise a trace-backed dynamic-verification-unavailable error."""
        trace_id = self._record(
            "infer_constructor_args",
            summary="dynamic verification unavailable: unsupported constructor args",
            degraded=True,
            contract=contract,
            argument=name,
            argument_type=typ,
            partial_constructor_args=partial_args,
            reason=reason,
        )
        raise SimulationDegradedError(
            f"dynamic_verification_unavailable: unsupported constructor argument "
            f"{name}:{typ} ({reason})",
            trace_id=trace_id,
        )

    def _record(self, tool: str, *, summary: str, **fields: Any) -> str:
        """Generate a trace id, log a structured trace event, and store it."""
        trace_id = uuid4().hex
        entry: dict[str, Any] = {"tool": tool, "summary": summary, **fields}
        self._traces[trace_id] = entry
        self._log.info("sim_trace", trace_id=trace_id, **entry)
        return trace_id

    def _degraded_error(
        self, tool: str, summary: str, exc: Exception, **fields: Any
    ) -> SimulationDegradedError:
        """Record a trace-backed degraded failure for an uncompleted tool call."""
        error = str(exc)
        trace_id = self._record(
            tool,
            summary=summary,
            degraded=True,
            error=error,
            **fields,
        )
        self._log.error(
            f"[DEGRADED] {tool} failed",
            trace_id=trace_id,
            error=error,
            **fields,
        )
        return SimulationDegradedError(error, trace_id=trace_id)

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        """Return a previously recorded trace by id (for replay), or None."""
        return self._traces.get(trace_id)
