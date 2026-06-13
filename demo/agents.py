"""Deterministic demo agents that drive the REAL MCP stack (architecture.md §12).

The hackathon demo must run end-to-end and reproducibly, but this environment
has no Qwen Cloud credentials, so these stand in for the LLM agents. They are
**not** scripted dialogue (CLAUDE.md Rule 1): each one makes real
``SimulationMCP``/``CodebaseMCP``/``MemoryMCP`` calls and builds its decision
from the *actual returned values* — every gas number, revert rate, and
``trace_id`` an Adversary cites is read out of a live Anvil run, never hardcoded.
What is deterministic is the control flow (which scenario to probe, when to
veto), not the evidence.

They satisfy the same graph-facing Protocols as the real
``YieldAgent``/``AdversaryAgent``/… (``src/sentinel/orchestrator/graph.py``), so
``WarRoomGraph`` runs unchanged; with ``QWEN_API_KEY`` set, the real agents drop
in via those identical Protocols (see ``run_demo.build_live_agents``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from sentinel.mcp_servers.codebase_mcp.engine import CodebaseEngine
from sentinel.mcp_servers.simulation_mcp.engine import SimulationEngine
from sentinel.memory.store import MemoryStore
from sentinel.orchestrator.schema import (
    AdversaryReview,
    BaselineAudit,
    Constraint,
    LessonsContext,
    Outcome,
    Proposal,
    RiskProfile,
    Severity,
    YieldAssessment,
    YieldVerdict,
)

# Model ids are display labels for the §9 tiering trace; the deterministic driver
# does not call them (token totals stay 0). With --live the real ids are used.
_DRIVER = "demo-driver"


@dataclass
class ProbeResult:
    """The real, simulation-derived outcome of one Adversary probe."""

    vetoed: bool
    severity: Severity | None
    reason: str
    evidence: str
    trace_ids: list[str]


class Probe(Protocol):
    """A real SimulationMCP experiment the Adversary runs to ground a claim."""

    def run(self, engine: SimulationEngine) -> ProbeResult:
        """Execute against the live fork and return the observed result."""
        ...


@dataclass
class RevertProbe:
    """Deploy a contract and measure its real revert rate under a scenario."""

    artifact: str
    ctor_args: list[object]
    scenario: str
    n: int
    label: str

    def run(self, engine: SimulationEngine) -> ProbeResult:
        """Deploy + ``get_revert_rate`` — veto if reverts exceed the 5% breaker."""
        deployed = engine.deploy_to_fork(self.artifact, self.ctor_args)
        rate = engine.get_revert_rate(deployed.address, self.scenario, self.n)
        vetoed = rate.revert_rate > 0.05
        severity = (
            Severity.HIGH
            if rate.revert_rate >= 0.5
            else Severity.MEDIUM
            if vetoed
            else None
        )
        verdict = "fails" if vetoed else "holds"
        return ProbeResult(
            vetoed=vetoed,
            severity=severity,
            reason=(
                f"{self.label} {verdict} under {self.scenario}: "
                f"{rate.revert_rate:.0%} of settlements revert"
            ),
            evidence=(
                f"get_revert_rate({self.scenario}) = {rate.revert_rate:.2f} "
                f"({rate.reverts}/{rate.n})"
            ),
            trace_ids=[rate.trace_id, deployed.trace_id],
        )


@dataclass
class AccessControlProbe:
    """Demonstrate a missing access check with a real unauthorized call."""

    artifact: str
    ctor_args: list[object]
    label: str

    def run(self, engine: SimulationEngine) -> ProbeResult:
        """Fund the vault, then call a privileged fn from an attacker account."""
        deployed = engine.deploy_to_fork(self.artifact, self.ctor_args)
        victim, attacker = engine.accounts[1], engine.accounts[2]
        engine.measure_gas(deployed.address, "deposit", [], value=10**18, sender=victim)
        call = engine.measure_gas(
            deployed.address, "setOperator", [attacker], sender=attacker
        )
        vetoed = not call.reverted  # an ungated privileged call simply succeeds
        return ProbeResult(
            vetoed=vetoed,
            severity=Severity.HIGH if vetoed else None,
            reason=(
                f"{self.label}: an unauthorized account reassigned the operator "
                f"role (call did not revert) — access control is missing"
                if vetoed
                else f"{self.label}: setOperator is correctly gated"
            ),
            evidence=f"measure_gas(setOperator, sender=attacker) reverted={call.reverted}",
            trace_ids=[call.trace_id, deployed.trace_id],
        )


class DemoYield:
    """Argues for shipping; cites real function names from CodebaseMCP (§4.1)."""

    model = _DRIVER

    def __init__(self, target: str, codebase: CodebaseEngine, key_function: str) -> None:
        self._target = target
        self._codebase = codebase
        self._key_function = key_function

    def assess(self, task: str) -> YieldAssessment:
        """Accept by default; reject batching (it breaks atomic settlement)."""
        functions = self._read_function_names()
        batching = "batch" in task.lower()
        verdict = YieldVerdict.REVISE if batching else YieldVerdict.ACCEPT
        rationale = (
            "batching breaks the atomic-settlement guarantee the merchant requires"
            if batching
            else f"{self._key_function} is bounded and ready to ship"
        )
        return YieldAssessment(
            run_id="demo",
            iteration=0,
            verdict=verdict,
            rationale=rationale,
            referenced_functions=functions,
        )

    def _read_function_names(self) -> list[str]:
        """Best-effort real read of the contract's functions (§4.1 guard)."""
        try:
            summary = self._codebase.read_contract(self._target).summary
            names = [f.name for f in summary.functions[:4]]
        except Exception:  # noqa: BLE001 — degrade to the known key function
            names = []
        return names or [self._key_function]


class DemoAdversary:
    """Vetoes only on real SimulationMCP evidence (§4.2, Golden Rule #1)."""

    model = _DRIVER

    def __init__(self, simulation: SimulationEngine, probes: list[Probe]) -> None:
        self._engine = simulation
        self._probes = probes
        self._i = 0

    def review(self, task: str) -> AdversaryReview:
        """Run the next real probe and build a sourced veto/clearance."""
        probe = self._probes[min(self._i, len(self._probes) - 1)]
        self._i += 1
        result = probe.run(self._engine)
        return AdversaryReview(
            run_id="demo",
            iteration=self._i,
            target_proposal_id="demo",
            vetoed=result.vetoed,
            severity=result.severity,
            reason=result.reason,
            evidence=result.evidence,
            trace_ids=result.trace_ids,
        )


@dataclass
class ProposalPlan:
    """A staged patch the Arbitrator proposes, grounded by a real probe."""

    patch_id: str
    summary: str
    grounding: Probe


@dataclass
class SynthPlan:
    """The Arbitrator's residual-risk narrative for the closing RiskProfile."""

    residual_risk_pct: float
    residual_risk_description: str
    mitigations_applied: list[str] = field(default_factory=list)


class DemoArbitrator:
    """Proposes patches and drafts the RiskProfile narrative (§4.3)."""

    model = _DRIVER

    def __init__(
        self,
        simulation: SimulationEngine,
        proposals: list[ProposalPlan],
        synth: SynthPlan,
    ) -> None:
        self._engine = simulation
        self._proposals = proposals
        self._synth = synth
        self._i = 0
        self._traces: list[str] = []

    def propose(self, task: str) -> Proposal:
        """Stage the next patch, grounded by a real simulation trace."""
        plan = self._proposals[min(self._i, len(self._proposals) - 1)]
        self._i += 1
        result = plan.grounding.run(self._engine)
        self._traces.extend(result.trace_ids)
        return Proposal(
            proposal_id=f"prop-{plan.patch_id}",
            run_id="demo",
            iteration=self._i,
            patch_id=plan.patch_id,
            summary=plan.summary,
            trace_ids=result.trace_ids,
        )

    def synthesize(self, task: str) -> RiskProfile:
        """Draft the risk narrative; the graph supplies authoritative accounting."""
        return RiskProfile(
            run_id="demo",
            outcome=Outcome.CONSTRAINTS_UNSATISFIED,
            residual_risk_pct=self._synth.residual_risk_pct,
            residual_risk_description=self._synth.residual_risk_description,
            mitigations_applied=self._synth.mitigations_applied,
            iterations=self._i,
            tokens_total=0,
            trace_ids=self._traces or ["demo-no-trace"],
        )


class DemoLessons:
    """Real MemoryMCP retrieval, reframed as constraints (§4.4, §6.1)."""

    model = _DRIVER

    def __init__(self, memory: MemoryStore, top_k: int = 3) -> None:
        self._memory = memory
        self._top_k = top_k

    def recall(self, task: str) -> LessonsContext:
        """Query the live store and reframe each lesson as a limitation."""
        scored = self._memory.query_memory(task, top_k=self._top_k)
        constraints = [
            Constraint(
                constraint_text=f"Constraint discovered the hard way: {s.record.lesson_text}",
                topic_tags=s.record.topic_tags,
                source_memory_id=s.record.id,
            )
            for s in scored
        ]
        return LessonsContext(
            constraints=constraints,
            memory_records_used=[s.record.id for s in scored],
        )


class DemoBaseline:
    """Single-pass control: static read only, no simulation, no memory (§4.5)."""

    model = _DRIVER

    def __init__(self, target: str, codebase: CodebaseEngine, audit: BaselineAudit) -> None:
        self._target = target
        self._codebase = codebase
        self._audit = audit

    def audit(self, task: str) -> BaselineAudit:
        """Read the contract once and return the canned single-pass finding."""
        try:
            self._codebase.read_contract(self._target)  # real read, no simulation
        except Exception:  # noqa: BLE001 — a baseline that cannot read still answers
            pass
        return self._audit
