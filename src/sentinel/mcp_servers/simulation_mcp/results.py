"""Typed result models for SimulationMCP tool calls (architecture.md §5.2, §10).

Every tool returns a result carrying a ``trace_id``. These trace ids are the
provenance that the Adversary Agent's ``Veto.trace_ids`` (and the Arbitrator's
``RiskProfile.trace_ids``) point back to — the concrete mechanism behind Golden
Rule #1: no claim without a real SimulationMCP call.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SimResult(BaseModel):
    """Base for all SimulationMCP results: every call is traceable."""

    trace_id: str


class DeployResult(SimResult):
    """Outcome of ``deploy_to_fork``."""

    contract: str
    address: str


class GasResult(SimResult):
    """Outcome of ``measure_gas`` — the actual gas a call consumed."""

    address: str
    function: str
    gas_used: int
    reverted: bool


class RevertRateResult(SimResult):
    """Outcome of ``get_revert_rate`` over ``n`` transactions."""

    address: str
    scenario: str
    n: int
    reverts: int
    revert_rate: float
    sample_errors: list[str] = Field(default_factory=list)


class TxSpikeResult(SimResult):
    """Outcome of ``run_tx_spike`` — a scripted load scenario."""

    address: str
    scenario: str
    txs_sent: int
    reverts: int
    revert_rate: float
    sample_errors: list[str] = Field(default_factory=list)


class ResetResult(SimResult):
    """Outcome of ``reset_fork``."""

    ok: bool
    error: str | None = None


class ExploitResult(SimResult):
    """Outcome of running a known exploit against one contract (architecture.md §5.2).

    ``exploited`` is the verdict: did the attack actually drain more than it
    staked (vulnerable) or did it revert (patched)?
    """

    exploit: str
    contract: str
    exploited: bool
    reverted: bool
    drained_wei: int


class PatchVerification(SimResult):
    """Before/after proof that a proposed patch closes a hole (architecture.md §5.2).

    Runs the same exploit against the vulnerable contract and the patched one.
    ``fix_verified`` is true only when the exploit succeeded on the former and
    failed on the latter — i.e. the patch demonstrably closed the hole, not just
    claimed to.
    """

    exploit: str
    vulnerable_contract: str
    fixed_contract: str
    before: ExploitResult
    after: ExploitResult
    fix_verified: bool
