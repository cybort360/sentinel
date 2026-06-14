"""Cross-component Pydantic models for the SENTINEL War Room.

These models are the single source of truth for data that crosses a module
boundary (architecture.md §6.2, §10). They are defined once here and imported
everywhere else — no ad-hoc dicts may cross a boundary (CLAUDE.md §5).

Golden Rule #1 (CLAUDE.md): every ``Proposal``, ``Veto``, and ``RiskProfile``
carries a non-empty ``trace_ids`` list pointing at logged ``SimulationMCP``
calls. The validators below make an *unsourced* claim impossible to construct,
which is the property ``tests/unit/test_no_unsourced_claims.py`` enforces.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator


class AgentRole(StrEnum):
    """The five SENTINEL agent identities (architecture.md §4)."""

    YIELD = "yield"
    ADVERSARY = "adversary"
    ARBITRATOR = "arbitrator"
    LESSONS = "lessons"
    BASELINE = "baseline"


class Severity(StrEnum):
    """Severity shared by vetoes and memory records (architecture.md §6.2)."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Outcome(StrEnum):
    """Terminal outcome of a War Room run (architecture.md §4.3, §10)."""

    CONSENSUS = "consensus"
    CONSTRAINTS_UNSATISFIED = "constraints_unsatisfied"


def _require_non_empty_trace_ids(value: list[str]) -> list[str]:
    """Enforce Golden Rule #1: a sourced claim needs ≥1 real trace id.

    Args:
        value: The candidate list of ``SimulationMCP`` trace ids.

    Returns:
        The validated list, unchanged.

    Raises:
        ValueError: If the list is empty or contains a blank entry.
    """
    if not value:
        raise ValueError(
            "trace_ids must contain at least one SimulationMCP trace_id "
            "(CLAUDE.md Golden Rule #1 — no unsourced agent claims)"
        )
    if any(not t.strip() for t in value):
        raise ValueError("trace_ids may not contain empty strings")
    return value


class Proposal(BaseModel):
    """A staged patch proposed by the Arbitrator during a War Room round.

    Mirrors ``CodebaseMCP.propose_patch`` output (architecture.md §8). The
    ``trace_ids`` tie the proposal to the simulation data that motivated it.
    """

    proposal_id: str
    run_id: str
    iteration: int = Field(ge=0)
    patch_id: str | None = None
    proposed_by: AgentRole = AgentRole.ARBITRATOR
    summary: str
    trace_ids: list[str]

    @field_validator("trace_ids")
    @classmethod
    def _check_trace_ids(cls, value: list[str]) -> list[str]:
        return _require_non_empty_trace_ids(value)


class Veto(BaseModel):
    """An evidence-backed objection to the current proposal (architecture.md §4.2).

    A veto without a ``SimulationMCP`` trace id is invalid and must be rejected
    by the Arbitrator — enforced here at construction time (Golden Rule #1).
    """

    veto_id: str
    run_id: str
    iteration: int = Field(ge=0)
    target_proposal_id: str
    issued_by: AgentRole
    severity: Severity
    reason: str
    evidence: str
    trace_ids: list[str]

    @field_validator("trace_ids")
    @classmethod
    def _check_trace_ids(cls, value: list[str]) -> list[str]:
        return _require_non_empty_trace_ids(value)


class YieldVerdict(StrEnum):
    """The Yield Agent's stance on the current contract/patch (architecture.md §4.1)."""

    ACCEPT = "accept"
    REVISE = "revise"
    REJECT = "reject"


class YieldAssessment(BaseModel):
    """The Yield Agent's output: an assessment that argues for shipping (§4.1).

    The Yield Agent has no veto power and makes no simulation claims, so it
    carries no ``trace_ids``. It *does* have to avoid the §4.1 failure mode —
    rubber-stamping — so ``referenced_functions`` must be non-empty: a verdict
    that cites no specific function is rejected at construction time, which is
    the structural analogue of "code review should reject vague Yield outputs."
    """

    run_id: str
    iteration: int = Field(ge=0)
    verdict: YieldVerdict
    rationale: str
    referenced_functions: list[str]
    proposed_parameters: dict[str, str] = Field(default_factory=dict)

    @field_validator("referenced_functions")
    @classmethod
    def _require_specific_references(cls, value: list[str]) -> list[str]:
        """Reject generic approval: a Yield verdict must cite real functions."""
        if not value or any(not f.strip() for f in value):
            raise ValueError(
                "referenced_functions must name ≥1 specific contract function "
                "(architecture.md §4.1 — no rubber-stamping / vague approvals)"
            )
        return value


class AdversaryReview(BaseModel):
    """The Adversary Agent's per-round decision: clear or veto (architecture.md §4.2).

    Golden Rule #1 applies symmetrically: a clean bill of health is as much a
    claim about ``SimulationMCP`` output as a veto is, so ``trace_ids`` is
    required and non-empty in *both* branches. When ``vetoed`` is true a
    ``severity`` must be supplied; :meth:`to_veto` then materialises the formal
    :class:`Veto` the Arbitrator consumes.
    """

    run_id: str
    iteration: int = Field(ge=0)
    target_proposal_id: str
    vetoed: bool
    severity: Severity | None = None
    reason: str
    evidence: str
    trace_ids: list[str]

    @field_validator("trace_ids")
    @classmethod
    def _check_trace_ids(cls, value: list[str]) -> list[str]:
        return _require_non_empty_trace_ids(value)

    @model_validator(mode="after")
    def _veto_needs_severity(self) -> AdversaryReview:
        """A veto must grade its own severity; a clearance must not."""
        if self.vetoed and self.severity is None:
            raise ValueError("a veto must specify a severity (architecture.md §4.2)")
        if not self.vetoed and self.severity is not None:
            raise ValueError("a clearance (vetoed=false) must not carry a severity")
        return self

    def to_veto(self, veto_id: str) -> Veto:
        """Materialise the formal :class:`Veto` for a vetoed review.

        Args:
            veto_id: Identifier to assign the produced veto.

        Returns:
            The corresponding :class:`Veto`.

        Raises:
            ValueError: If called on a non-veto (cleared) review.
        """
        if not self.vetoed or self.severity is None:
            raise ValueError("to_veto() is only valid on a vetoed review")
        return Veto(
            veto_id=veto_id,
            run_id=self.run_id,
            iteration=self.iteration,
            target_proposal_id=self.target_proposal_id,
            issued_by=AgentRole.ADVERSARY,
            severity=self.severity,
            reason=self.reason,
            evidence=self.evidence,
            trace_ids=self.trace_ids,
        )


class Resolution(StrEnum):
    """How a round's Yield-vs-Adversary conflict was resolved (architecture.md §4.3)."""

    RECONCILED = "reconciled"  # consensus — Yield accepts, Adversary clears
    VETO_UPHELD = "veto_upheld"  # Adversary's evidence overrides Yield; revise
    REVISION_REQUIRED = "revision_required"  # Yield itself wants changes (no veto)
    UNRESOLVED = "unresolved"  # final round, no joint solution — deadlock


class RoundAdjudication(BaseModel):
    """The Arbitrator's ruling on one round's Yield-vs-Adversary conflict (§4.3).

    Makes conflict resolution explicit and recordable — the Track 3 "how they
    resolve disagreements and execution conflicts" requirement. Like every claim
    object it is *sourced*: the ruling cites the round's ``SimulationMCP``
    evidence (the Adversary's ``trace_ids``), so an unsourced adjudication cannot
    be constructed (Golden Rule #1).
    """

    run_id: str
    iteration: int = Field(ge=0)
    yield_verdict: YieldVerdict
    adversary_vetoed: bool
    resolution: Resolution
    rationale: str
    trace_ids: list[str]

    @field_validator("trace_ids")
    @classmethod
    def _check_trace_ids(cls, value: list[str]) -> list[str]:
        return _require_non_empty_trace_ids(value)


class Constraint(BaseModel):
    """A single retrieved lesson, reframed as a constraint (architecture.md §6.1).

    The Track 1 design intent: a memory is a limitation discovered the hard way,
    **not** a ready-made fix. ``constraint_text`` is phrased as something the
    proposal must now satisfy — never as the answer.
    """

    constraint_text: str
    topic_tags: list[str]
    source_memory_id: str


class LessonsContext(BaseModel):
    """The Lessons Agent's injection: prior incidents as live constraints (§4.4).

    Carries no ``trace_ids`` — memory recall is not a simulation claim. The
    constraints are what make the Arbitrator's job *harder*, which is the
    Track 1 differentiator (architecture.md §6.1).
    """

    constraints: list[Constraint] = Field(default_factory=list)
    memory_records_used: list[str] = Field(default_factory=list)


class BaselineAudit(BaseModel):
    """The Baseline Agent's single-pass output (architecture.md §4.5, §11).

    Exists only for the efficiency comparison: one shot, ``CodebaseMCP`` read
    access only, no simulation and no negotiation — hence no ``trace_ids``. The
    expected story (§11) is that this misses the latent trade-off the War Room
    surfaces.
    """

    vulnerabilities: list[str] = Field(default_factory=list)
    recommended_fix: str
    referenced_functions: list[str] = Field(default_factory=list)
    residual_risk_disclosed: bool = False


class RiskProfile(BaseModel):
    """The Arbitrator's structured run output (architecture.md §10).

    Always produced, whether the outcome is consensus or unresolved tension.
    ``trace_ids`` link the residual-risk numbers back to the ``SimulationMCP``
    runs that produced them (Golden Rule #1). Even an "empty" early-exit profile
    (architecture.md §9) cites the initial-scan traces that justify a 0% risk.
    """

    run_id: str
    outcome: Outcome
    final_proposal: str | None = None
    residual_risk_pct: float = Field(ge=0.0, le=1.0)
    residual_risk_description: str
    mitigations_applied: list[str] = Field(default_factory=list)
    memory_records_used: list[str] = Field(default_factory=list)
    iterations: int = Field(ge=0)
    tokens_total: int = Field(ge=0)
    trace_ids: list[str]

    @field_validator("trace_ids")
    @classmethod
    def _check_trace_ids(cls, value: list[str]) -> list[str]:
        return _require_non_empty_trace_ids(value)
