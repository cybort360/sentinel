"""War Room orchestration graph (architecture.md §8, budget rules §9).

Implements the control flow: INGEST → parallel INITIAL SCAN → WAR ROOM LOOP
(hard-capped at ``budget.max_iterations``) → RISK PROFILE — always producing a
``RiskProfile`` whether the run ends in consensus or constraints-unsatisfied. The
graph is the source of truth for *accounting* fields (outcome, iterations,
tokens, accumulated ``trace_ids`` and memory ids); each agent owns only its own
*judgment*. That split is what keeps Golden Rule #1 honest at the run level: the
profile's ``trace_ids`` are the real ``SimulationMCP`` traces the Adversary
produced, not whatever the synthesizing model echoes back.

Agents are reached through narrow Protocols, so the whole loop is unit-testable
with scripted fakes — no LLM, no MCP servers (see ``tests/unit/test_graph.py``).
The human checkpoint (§7) and patch apply (§8 step 6) sit *after* ``run`` returns
its :class:`RunResult`; this module deliberately stops at the RiskProfile so the
blocking gate is never bypassed from inside the loop (Rule 2).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, Field

from sentinel.mcp_servers.codebase_mcp.results import ProposeResult
from sentinel.observability.trace_logger import DemoTag, emit, get_logger
from sentinel.orchestrator.schema import (
    AdversaryReview,
    BaselineAudit,
    Constraint,
    DynamicVerificationStatus,
    LessonsContext,
    Outcome,
    Proposal,
    Resolution,
    RiskProfile,
    RoundAdjudication,
    Severity,
    Veto,
    YieldAssessment,
    YieldVerdict,
)
from sentinel.orchestrator.token_budget import BudgetConfig, TokenLedger

_log = get_logger("orchestrator")


class _AgentLike(Protocol):
    """Shared surface: every agent exposes the model id it calls (§9 tiering)."""

    @property
    def model(self) -> str:
        """The model id this agent calls (read-only; a plain attr also satisfies)."""
        ...


class YieldLike(_AgentLike, Protocol):
    """Graph-facing Yield Agent surface (architecture.md §4.1)."""

    def assess(self, task: str) -> YieldAssessment:
        """Return the Yield Agent's shipping assessment for ``task``."""
        ...


class AdversaryLike(_AgentLike, Protocol):
    """Graph-facing Adversary Agent surface (architecture.md §4.2)."""

    def review(self, task: str) -> AdversaryReview:
        """Return the Adversary's evidence-backed veto/clearance for ``task``."""
        ...


class ArbitratorLike(_AgentLike, Protocol):
    """Graph-facing Arbitrator surface (architecture.md §4.3)."""

    def propose(self, task: str) -> Proposal:
        """Stage and return a candidate patch for this round."""
        ...

    def synthesize(self, task: str) -> RiskProfile:
        """Draft the run-terminating risk narrative."""
        ...


class LessonsLike(_AgentLike, Protocol):
    """Graph-facing Lessons Agent surface (architecture.md §4.4)."""

    def recall(self, task: str) -> LessonsContext:
        """Return prior incidents reframed as live constraints."""
        ...


class BaselineLike(_AgentLike, Protocol):
    """Graph-facing Baseline Agent surface (architecture.md §4.5)."""

    def audit(self, task: str) -> BaselineAudit:
        """Return a single-pass audit of the original contract."""
        ...


class ContractReader(Protocol):
    """The ingest surface the graph needs from CodebaseMCP (architecture.md §5.1)."""

    def read_contract(self, path: str) -> object:
        """Return source + a parsed summary; may expose ``unauditable`` data."""
        ...


@dataclass
class _Scan:
    """The three independent initial-scan results (architecture.md §8 step 2)."""

    assessment: YieldAssessment
    review: AdversaryReview
    lessons: LessonsContext


@dataclass
class _LoopResult:
    """Outcome of the War Room loop (architecture.md §8 step 3)."""

    outcome: Outcome
    final_proposal: Proposal | None
    assessment: YieldAssessment
    review: AdversaryReview
    iterations: int
    trace_ids: list[str]
    memory_ids: list[str]
    # Records recalled in response to a veto (architecture.md §8 step e/f, §12
    # Round 2) — the finding-driven retrieval, kept apart from the ingest-time
    # "topic guess" so §6.4 can showcase the memory that actually constrained
    # the audit, not the pre-simulation prefetch.
    finding_memory_ids: list[str]
    # The Arbitrator's per-round conflict rulings (architecture.md §4.3) — the
    # negotiation transcript surfaced for the human gate / UI (Track 3).
    negotiation: list[RoundAdjudication]
    # Tool/orchestration failure that prevented a protocol-level disagreement
    # from being evaluated. This is not an Adversary veto.
    tool_failure_reason: str | None = None
    dynamic_verification_reason: str | None = None


class RunResult(BaseModel):
    """Everything the human checkpoint / demo needs from one audit run.

    ``risk_profile`` is always present (§8 step 4). ``final_proposal`` is the
    staged patch awaiting the human gate (§7) — never auto-applied here.
    ``final_assessment``/``final_review`` are the closing Yield/Adversary
    positions the §7.2 Decision Packet renders for the human.
    """

    run_id: str
    target: str
    risk_profile: RiskProfile
    final_proposal: Proposal | None = None
    final_assessment: YieldAssessment | None = None
    final_review: AdversaryReview | None = None
    unauditable_functions: list[str] = Field(default_factory=list)
    baseline: BaselineAudit | None = None
    # The prior incident(s) the War Room recalled *in response to a finding*
    # (the §6.4 cross-session signal), distinct from the weaker ingest-time
    # topic-guess folded into ``risk_profile.memory_records_used``.
    finding_memory_records: list[str] = Field(default_factory=list)
    # The Arbitrator's per-round conflict rulings — the negotiation transcript
    # (architecture.md §4.3); empty on a clean early-exit (no conflict arose).
    negotiation: list[RoundAdjudication] = Field(default_factory=list)


def _annotate_residual_risk(
    profile: RiskProfile, *, headline_trace_id: str | None = None
) -> None:
    """Emit the §10 ``[ANNOTATED RESIDUAL RISK]`` line from the RiskProfile.

    Rendered from the structured profile (Rule 1: cites a real trace id), not
    written as separate narration.
    """
    if (
        profile.outcome is Outcome.DYNAMIC_VERIFICATION_UNAVAILABLE
        or profile.dynamic_verification_status
        in (
            DynamicVerificationStatus.UNAVAILABLE,
            DynamicVerificationStatus.INCOMPLETE,
        )
    ):
        label = "Unverified"
    else:
        label = f"{profile.residual_risk_pct:.0%}"
    emit(
        _log,
        DemoTag.ANNOTATED_RESIDUAL_RISK,
        f"{label} - {profile.residual_risk_description}",
        run_id=profile.run_id,
        agent="arbitrator",
        action="risk_profile",
        trace_id=headline_trace_id or profile.trace_ids[0],
        residual_risk_pct=profile.residual_risk_pct,
        outcome=profile.outcome.value,
    )


def _adjudicate(
    assessment: YieldAssessment,
    review: AdversaryReview,
    run_id: str,
    iteration: int,
    *,
    consensus: bool,
    is_final: bool,
) -> RoundAdjudication:
    """Build + emit the Arbitrator's ruling on one round's conflict (§4.3, §10).

    This is the Track 3 "resolve disagreements and execution conflicts" step made
    explicit. The resolution is *derived* from the round's real positions and
    evidence, never invented: a consensus reconciles the two agents; a standing
    veto is upheld (or, on the final round, leaves the conflict unresolved — a
    deadlock); a no-veto non-accept verdict asks for revision. The ruling cites
    the round's ``SimulationMCP`` evidence (the Adversary's ``trace_ids``), so it
    is sourced like every other claim (Golden Rule #1).
    """
    if consensus:
        resolution = Resolution.RECONCILED
        rationale = (
            "Yield accepts and the Adversary clears the patch — positions reconciled."
        )
    elif review.vetoed and is_final:
        resolution = Resolution.UNRESOLVED
        rationale = (
            f"No patch jointly satisfies Yield's ship goal and the Adversary's veto "
            f"({review.reason}); deadlock — residual risk to be disclosed."
        )
    elif review.vetoed:
        resolution = Resolution.VETO_UPHELD
        rationale = (
            f"Adversary veto upheld over Yield's '{assessment.verdict.value}' verdict: "
            f"{review.reason}. The next round must satisfy this constraint."
        )
    else:
        resolution = Resolution.REVISION_REQUIRED
        rationale = (
            f"No veto, but Yield's verdict is '{assessment.verdict.value}': "
            f"{assessment.rationale}. Revision required before shipping."
        )
    adjudication = RoundAdjudication(
        run_id=run_id,
        iteration=iteration,
        yield_verdict=assessment.verdict,
        adversary_vetoed=review.vetoed,
        resolution=resolution,
        rationale=rationale,
        trace_ids=list(review.trace_ids),
    )
    emit(
        _log,
        DemoTag.ARBITRATION,
        f"round {iteration}: {resolution.value.replace('_', ' ')} — {rationale}",
        run_id=run_id,
        agent="arbitrator",
        action="adjudicate",
        trace_id=review.trace_ids[0],
        iteration=iteration,
        resolution=resolution.value,
    )
    return adjudication


def _emit_proposal(proposal: Proposal, run_id: str, iteration: int) -> None:
    """Emit an Arbitrator-lane trace event for the staged patch (architecture.md §18).

    The Arbitrator stages each round's candidate patch (architecture.md §4.3). The
    ``Proposal`` already carries the real ``SimulationMCP`` ``trace_ids`` that
    motivated it (Golden Rule #1), so this surfaces the first as a clickable
    evidence chip. Routed to the Arbitrator swimlane via ``agent``; not a demo tag.
    """
    _log.info(
        proposal.summary,
        run_id=run_id,
        agent="arbitrator",
        action="propose",
        iteration=iteration,
        trace_id=proposal.trace_ids[0],
        patch_id=proposal.patch_id,
    )


def _source_proposal(proposal: Proposal, known_trace_ids: list[str]) -> Proposal:
    """Keep proposal evidence tied to real traces already produced in this run."""
    known = set(known_trace_ids)
    valid = [trace_id for trace_id in proposal.trace_ids if trace_id in known]
    if valid:
        return proposal.model_copy(update={"trace_ids": valid})
    fallback = known_trace_ids[-1:]
    _log.debug(
        "arbitrator proposal cited unknown trace_ids",
        proposal_id=proposal.proposal_id,
        cited=proposal.trace_ids,
        fallback=fallback,
    )
    return proposal.model_copy(update={"trace_ids": fallback})


def _residual_risk_description(loop: _LoopResult) -> str:
    """Describe residual risk from the final agent positions, not free prose."""
    if loop.outcome is Outcome.TOOL_FAILURE:
        return loop.tool_failure_reason or "War Room stopped after tool failure."
    if loop.outcome is Outcome.DYNAMIC_VERIFICATION_UNAVAILABLE:
        return (
            "Dynamic verification was unavailable, so the final proposal remains "
            f"static-only: {loop.dynamic_verification_reason or loop.review.reason}"
        )
    if loop.outcome is Outcome.CONSENSUS:
        return (
            f"Final Adversary review cleared the proposal: {loop.review.reason}. "
            f"Evidence: {loop.review.evidence}"
        )
    if loop.review.vetoed:
        return (
            f"Final Adversary veto remains unresolved: {loop.review.reason}. "
            f"Evidence: {loop.review.evidence}"
        )
    return (
        f"Final review did not veto, but Yield did not accept the proposal: "
        f"{loop.assessment.rationale}. Adversary evidence: {loop.review.evidence}"
    )


def _missing_patch_assessment(
    proposal: Proposal, run_id: str, iteration: int
) -> YieldAssessment:
    """Yield cannot evaluate a proposal that never staged a patch."""
    return YieldAssessment(
        run_id=run_id,
        iteration=iteration,
        verdict=YieldVerdict.REJECT,
        rationale=(
            f"Proposal {proposal.proposal_id} did not produce a staged patch, "
            "so there is no implementation to evaluate for deployment."
        ),
        referenced_functions=["proposal_staging"],
    )


def _missing_patch_review(
    proposal: Proposal, run_id: str, iteration: int
) -> AdversaryReview:
    """Adversary veto for a proposal with no staged patch artifact."""
    return AdversaryReview(
        run_id=run_id,
        iteration=iteration,
        target_proposal_id=proposal.proposal_id,
        vetoed=True,
        severity=Severity.HIGH,
        reason=(
            f"Proposal {proposal.proposal_id} did not produce a patch_id, so no "
            "staged source exists to deploy or simulate."
        ),
        evidence="The Proposal.patch_id field is empty after the staging step.",
        trace_ids=list(proposal.trace_ids),
    )


def _emit_yield_assessment(
    assessment: YieldAssessment, run_id: str, iteration: int
) -> None:
    """Emit a Yield-lane trace event for the War Room UI (architecture.md §18).

    The Yield Agent argues for shipping and makes *no* simulation claims, so this
    event carries no ``trace_id`` — it is a judgment, not a Rule-1 measurement
    (consistent with ``YieldAssessment`` carrying no ``trace_ids``). ``agent``
    routes it to the Yield swimlane; it is intentionally *not* one of the five
    §10 demo tags, so it is a plain structured event, not a headline claim.
    """
    _log.info(
        f"{assessment.verdict.value}: {assessment.rationale}",
        run_id=run_id,
        agent="yield",
        action=assessment.verdict.value,
        iteration=iteration,
        referenced_functions=assessment.referenced_functions,
    )


def _dedup(items: list[str]) -> list[str]:
    """Order-preserving de-duplication for accumulated ids."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


class WarRoomGraph:
    """Drives one contract through the War Room (architecture.md §8)."""

    def __init__(
        self,
        *,
        yield_agent: YieldLike,
        adversary: AdversaryLike,
        arbitrator: ArbitratorLike,
        lessons: LessonsLike,
        codebase: ContractReader,
        budget: BudgetConfig | None = None,
        baseline: BaselineLike | None = None,
    ) -> None:
        """Wire the graph to its agents and budget.

        Args:
            yield_agent: The Yield Agent (argues for shipping).
            adversary: The Adversary Agent (evidence-backed veto).
            arbitrator: The Arbitrator (proposes patches, synthesizes profile).
            lessons: The Lessons Agent (memory as constraints).
            codebase: CodebaseMCP read surface for ingest.
            budget: §9 budget knobs; defaults to ``BudgetConfig.from_env()``.
            baseline: Optional Baseline Agent for the §11 comparison (§8 step 7).
        """
        self._yield = yield_agent
        self._adversary = adversary
        self._arbitrator = arbitrator
        self._lessons = lessons
        self._codebase = codebase
        self._baseline = baseline
        self._budget = budget or BudgetConfig.from_env()

    def run(self, target: str, run_id: str | None = None) -> RunResult:
        """Audit ``target`` end-to-end, returning the run's :class:`RunResult`.

        Args:
            target: Path to the contract under audit.
            run_id: Optional run id; a uuid is generated if omitted.

        Returns:
            The :class:`RunResult` (always carrying a ``RiskProfile``) for the
            human checkpoint to act on.
        """
        run_id = run_id or str(uuid4())
        ledger = TokenLedger()
        emit(
            _log,
            DemoTag.SYSTEM_DECISION,
            f"run start — auditing {target}",
            run_id=run_id,
            agent="orchestrator",
            action="run_start",
        )

        unauditable = self._ingest(target)
        scan = self._initial_scan(target, ledger)
        _emit_yield_assessment(scan.assessment, run_id, 0)
        trace_ids = list(scan.review.trace_ids)
        memory_ids = list(scan.lessons.memory_records_used)

        if self._is_clean_consensus(scan):
            emit(
                _log,
                DemoTag.SYSTEM_DECISION,
                "early exit — initial scan found no anomalies",
                run_id=run_id,
                agent="orchestrator",
                action="early_exit",
                trace_id=scan.review.trace_ids[0],
            )
            profile = self._empty_profile(run_id, ledger, trace_ids, memory_ids)
            return self._result(
                run_id,
                target,
                profile,
                None,
                scan.assessment,
                scan.review,
                unauditable,
                ledger,
            )

        loop = self._war_room(target, run_id, scan, ledger, trace_ids, memory_ids)
        profile = self._risk_profile(run_id, loop, ledger)
        return self._result(
            run_id,
            target,
            profile,
            loop.final_proposal,
            loop.assessment,
            loop.review,
            unauditable,
            ledger,
            finding_memory_records=loop.finding_memory_ids,
            negotiation=loop.negotiation,
        )

    # -- step 1: ingest ---------------------------------------------------- #

    def _ingest(self, target: str) -> list[str]:
        """Read the target, flagging unauditable functions (Rule 4 / Track 4)."""
        try:
            result = self._codebase.read_contract(target)
        except Exception as exc:  # noqa: BLE001 — degrade, don't crash the run
            _log.error(
                "[DEGRADED] ingest read_contract failed", target=target, error=str(exc)
            )
            return []
        summary = getattr(result, "summary", None)
        unauditable: list[str] = list(getattr(summary, "unauditable_functions", []))
        if unauditable:
            _log.warning("ingest flagged unauditable functions", functions=unauditable)
        return unauditable

    # -- step 2: parallel initial scan ------------------------------------- #

    def _initial_scan(self, target: str, ledger: TokenLedger) -> _Scan:
        """Run Yield/Adversary/Lessons concurrently (architecture.md §8 step 2)."""
        with ThreadPoolExecutor(max_workers=3) as pool:
            f_yield = pool.submit(self._yield.assess, _initial_yield_task(target))
            f_adv = pool.submit(self._adversary.review, _initial_adversary_task(target))
            f_lessons = pool.submit(self._lessons.recall, _initial_lessons_task(target))
            assessment = f_yield.result()
            review = f_adv.result()
            lessons = f_lessons.result()
        ledger.record("yield", "initial_assess", self._yield.model)
        ledger.record(
            "adversary",
            "initial_scan",
            self._adversary.model,
            trace_id=review.trace_ids[0],
        )
        ledger.record("lessons", "initial_query", self._lessons.model)
        return _Scan(assessment=assessment, review=review, lessons=lessons)

    @staticmethod
    def _is_clean_consensus(scan: _Scan) -> bool:
        """Early-exit test (§9): clean simulation AND Yield accepts the original."""
        return not scan.review.vetoed and scan.assessment.verdict is YieldVerdict.ACCEPT

    # -- step 3: war room loop --------------------------------------------- #

    def _war_room(
        self,
        target: str,
        run_id: str,
        scan: _Scan,
        ledger: TokenLedger,
        trace_ids: list[str],
        memory_ids: list[str],
    ) -> _LoopResult:
        """Iterate propose → evaluate → simulate until consensus or the cap."""
        constraints = list(scan.lessons.constraints)
        finding_memory_ids: list[str] = []
        negotiation: list[RoundAdjudication] = []
        last_veto = self._initial_veto(scan, run_id)
        final_proposal: Proposal | None = None
        # Seed with the initial-scan positions so they are always bound; each
        # round overwrites them with that round's closing positions (§7.2).
        assessment = scan.assessment
        review = scan.review
        outcome = Outcome.CONSTRAINTS_UNSATISFIED
        iterations = 0

        for n in range(1, self._budget.max_iterations + 1):
            iterations = n
            latest_patch_before = self._latest_patch_id()
            try:
                proposal = self._arbitrator.propose(
                    _propose_task(target, run_id, n, last_veto, constraints)
                )
            except Exception as exc:  # noqa: BLE001 — terminal tool failure
                reason = f"Patch staging failed before proposal creation: {exc}"
                _log.error(
                    "[DEGRADED] arbitrator propose failed",
                    run_id=run_id,
                    iteration=n,
                    error=str(exc),
                )
                emit(
                    _log,
                    DemoTag.SYSTEM_DECISION,
                    f"[DEGRADED] {reason}",
                    run_id=run_id,
                    agent="orchestrator",
                    action="tool_failure",
                    trace_id=trace_ids[-1] if trace_ids else None,
                    iteration=n,
                )
                return _LoopResult(
                    outcome=Outcome.TOOL_FAILURE,
                    final_proposal=None,
                    assessment=assessment,
                    review=review,
                    iterations=n,
                    trace_ids=trace_ids,
                    memory_ids=memory_ids + finding_memory_ids,
                    finding_memory_ids=finding_memory_ids,
                    negotiation=negotiation,
                    tool_failure_reason=reason,
                )
            proposal = self._attach_latest_patch_result(
                _source_proposal(proposal, trace_ids), latest_patch_before
            )
            proposal = self._attach_staged_artifact(target, proposal)
            ledger.record("arbitrator", f"propose_v{n}", self._arbitrator.model)

            if proposal.patch_id is None:
                error = self._latest_patch_error()
                reason = (
                    f"Patch staging failed for proposal {proposal.proposal_id}: "
                    "proposal has no patch_id, so no staged source exists."
                )
                if error:
                    reason = f"{reason} CodebaseMCP.propose_patch error: {error}"
                _log.error(
                    "[DEGRADED] arbitrator proposal missing patch_id",
                    proposal_id=proposal.proposal_id,
                    trace_id=proposal.trace_ids[0],
                    propose_patch_error=error,
                )
                emit(
                    _log,
                    DemoTag.SYSTEM_DECISION,
                    f"[DEGRADED] {reason}",
                    run_id=run_id,
                    agent="orchestrator",
                    action="tool_failure",
                    trace_id=proposal.trace_ids[0],
                    iteration=n,
                )
                return _LoopResult(
                    outcome=Outcome.TOOL_FAILURE,
                    final_proposal=None,
                    assessment=assessment,
                    review=review,
                    iterations=n,
                    trace_ids=trace_ids,
                    memory_ids=memory_ids + finding_memory_ids,
                    finding_memory_ids=finding_memory_ids,
                    negotiation=negotiation,
                    tool_failure_reason=reason,
                )

            trace_ids += proposal.trace_ids
            _emit_proposal(proposal, run_id, n)
            final_proposal = proposal
            assessment = self._yield.assess(_evaluate_task(proposal))
            ledger.record("yield", f"evaluate_v{n}", self._yield.model)
            _emit_yield_assessment(assessment, run_id, n)
            review = self._adversary.review(_review_task(target, proposal))
            ledger.record(
                "adversary",
                f"simulate_v{n}",
                self._adversary.model,
                trace_id=review.trace_ids[0],
            )
            trace_ids += review.trace_ids

            if _is_dynamic_verification_unavailable(review):
                reason = (
                    "Dynamic verification unavailable for the staged proposal: "
                    f"{review.reason}"
                )
                _log.warning(
                    "dynamic verification unavailable",
                    run_id=run_id,
                    iteration=n,
                    trace_id=review.trace_ids[0],
                    reason=review.reason,
                )
                emit(
                    _log,
                    DemoTag.SYSTEM_DECISION,
                    reason,
                    run_id=run_id,
                    agent="orchestrator",
                    action="dynamic_verification_unavailable",
                    trace_id=review.trace_ids[0],
                    iteration=n,
                )
                return _LoopResult(
                    outcome=Outcome.DYNAMIC_VERIFICATION_UNAVAILABLE,
                    final_proposal=proposal,
                    assessment=assessment,
                    review=review,
                    iterations=n,
                    trace_ids=trace_ids,
                    memory_ids=memory_ids + finding_memory_ids,
                    finding_memory_ids=finding_memory_ids,
                    negotiation=negotiation,
                    dynamic_verification_reason=reason,
                )

            if _is_patch_verification_tool_failure(review):
                reason = (
                    "Patch verification failed before protocol risk could be "
                    f"adjudicated: {review.reason}"
                )
                _log.error(
                    "[DEGRADED] patch verification tool failure",
                    run_id=run_id,
                    iteration=n,
                    trace_id=review.trace_ids[0],
                    reason=review.reason,
                )
                emit(
                    _log,
                    DemoTag.SYSTEM_DECISION,
                    f"[DEGRADED] {reason}",
                    run_id=run_id,
                    agent="orchestrator",
                    action="tool_failure",
                    trace_id=review.trace_ids[0],
                    iteration=n,
                )
                return _LoopResult(
                    outcome=Outcome.TOOL_FAILURE,
                    final_proposal=proposal,
                    assessment=assessment,
                    review=review,
                    iterations=n,
                    trace_ids=trace_ids,
                    memory_ids=memory_ids + finding_memory_ids,
                    finding_memory_ids=finding_memory_ids,
                    negotiation=negotiation,
                    tool_failure_reason=reason,
                    dynamic_verification_reason=None,
                )

            consensus = assessment.verdict is YieldVerdict.ACCEPT and not review.vetoed
            is_final = n == self._budget.max_iterations
            negotiation.append(
                _adjudicate(
                    assessment,
                    review,
                    run_id,
                    n,
                    consensus=consensus,
                    is_final=is_final,
                )
            )

            if consensus:
                outcome = Outcome.CONSENSUS
                emit(
                    _log,
                    DemoTag.SYSTEM_DECISION,
                    f"consensus reached on patch {proposal.patch_id}",
                    run_id=run_id,
                    agent="orchestrator",
                    action="consensus",
                    trace_id=review.trace_ids[0],
                    iteration=n,
                )
                break

            last_veto = self._handle_veto(
                review, run_id, n, ledger, finding_memory_ids, constraints
            )

        if outcome is Outcome.CONSTRAINTS_UNSATISFIED:
            emit(
                _log,
                DemoTag.CONSTRAINTS_UNSATISFIED,
                f"no joint solution after {iterations} iterations",
                run_id=run_id,
                agent="arbitrator",
                action="terminate",
                trace_id=review.trace_ids[0],
                iterations=iterations,
            )
        return _LoopResult(
            outcome=outcome,
            final_proposal=final_proposal,
            assessment=assessment,
            review=review,
            iterations=iterations,
            trace_ids=trace_ids,
            # Full accounting = ingest topic-guess + every finding-driven recall.
            memory_ids=memory_ids + finding_memory_ids,
            finding_memory_ids=finding_memory_ids,
            negotiation=negotiation,
            tool_failure_reason=None,
            dynamic_verification_reason=None,
        )

    @staticmethod
    def _initial_veto(scan: _Scan, run_id: str) -> Veto | None:
        """Carry the initial-scan veto (if any) into the first proposal's context."""
        if scan.review.vetoed:
            return scan.review.to_veto(f"veto-{run_id}-0")
        return None

    def _handle_veto(
        self,
        review: AdversaryReview,
        run_id: str,
        n: int,
        ledger: TokenLedger,
        finding_memory_ids: list[str],
        constraints: list[Constraint],
    ) -> Veto | None:
        """On a veto, pull relevant memory and inject it as new constraints (§8 e/f).

        The records recalled here are *finding-driven* (queried with the veto's
        observed failure mode), so they discriminate by failure — unlike the
        ingest-time topic guess. They accumulate into ``finding_memory_ids`` for
        the §6.4 cross-session showcase.
        """
        if not review.vetoed:
            return None
        veto = review.to_veto(f"veto-{run_id}-{n}")
        emit(
            _log,
            DemoTag.ADVERSARY_VETO,
            veto.reason,
            run_id=run_id,
            agent="adversary",
            action="veto",
            trace_id=veto.trace_ids[0],
            iteration=n,
        )
        recalled = self._lessons.recall(_memory_task(veto))
        ledger.record("lessons", f"recall_v{n}", self._lessons.model)
        finding_memory_ids += recalled.memory_records_used
        constraints += recalled.constraints
        return veto

    # -- step 4: risk profile ---------------------------------------------- #

    def _risk_profile(
        self, run_id: str, loop: _LoopResult, ledger: TokenLedger
    ) -> RiskProfile:
        """Build the final RiskProfile: Arbitrator narrative + graph accounting."""
        if loop.outcome is Outcome.TOOL_FAILURE:
            profile = RiskProfile(
                run_id=run_id,
                outcome=Outcome.TOOL_FAILURE,
                final_proposal=None,
                residual_risk_pct=0.0,
                residual_risk_description=_residual_risk_description(loop),
                mitigations_applied=[],
                memory_records_used=_dedup(loop.memory_ids),
                iterations=loop.iterations,
                tokens_total=ledger.total,
                trace_ids=_dedup(loop.trace_ids),
                dynamic_verification_status=DynamicVerificationStatus.FAILED,
            )
            _annotate_residual_risk(
                profile,
                headline_trace_id=loop.trace_ids[-1] if loop.trace_ids else None,
            )
            return profile
        if loop.outcome is Outcome.DYNAMIC_VERIFICATION_UNAVAILABLE:
            profile = RiskProfile(
                run_id=run_id,
                outcome=Outcome.DYNAMIC_VERIFICATION_UNAVAILABLE,
                final_proposal=(
                    loop.final_proposal.patch_id if loop.final_proposal else None
                ),
                residual_risk_pct=0.0,
                residual_risk_description=_residual_risk_description(loop),
                mitigations_applied=[],
                memory_records_used=_dedup(loop.memory_ids),
                iterations=loop.iterations,
                tokens_total=ledger.total,
                trace_ids=_dedup(loop.trace_ids),
                dynamic_verification_status=DynamicVerificationStatus.UNAVAILABLE,
            )
            _annotate_residual_risk(
                profile,
                headline_trace_id=loop.review.trace_ids[0],
            )
            return profile
        draft = self._arbitrator.synthesize(
            _synthesis_task(run_id, loop.outcome, loop.final_proposal, loop.iterations)
        )
        ledger.record("arbitrator", "synthesize", self._arbitrator.model)
        profile = RiskProfile(
            run_id=run_id,
            outcome=loop.outcome,
            final_proposal=(
                loop.final_proposal.patch_id if loop.final_proposal else None
            ),
            residual_risk_pct=draft.residual_risk_pct,
            residual_risk_description=_residual_risk_description(loop),
            mitigations_applied=draft.mitigations_applied,
            memory_records_used=_dedup(loop.memory_ids),
            iterations=loop.iterations,
            tokens_total=ledger.total,
            trace_ids=_dedup(loop.trace_ids),
            dynamic_verification_status=DynamicVerificationStatus.PASSED,
        )
        _annotate_residual_risk(profile, headline_trace_id=loop.review.trace_ids[0])
        return profile

    def _empty_profile(
        self,
        run_id: str,
        ledger: TokenLedger,
        trace_ids: list[str],
        memory_ids: list[str],
    ) -> RiskProfile:
        """Lightweight early-exit sign-off (§9): empty profile, real scan traces."""
        profile = RiskProfile(
            run_id=run_id,
            outcome=Outcome.CONSENSUS,
            final_proposal=None,
            residual_risk_pct=0.0,
            residual_risk_description=(
                "Initial SimulationMCP scan found no anomalies; no patch required "
                "(early exit, architecture.md §9)."
            ),
            mitigations_applied=[],
            memory_records_used=_dedup(memory_ids),
            iterations=0,
            tokens_total=ledger.total,
            trace_ids=_dedup(trace_ids),
            dynamic_verification_status=DynamicVerificationStatus.PASSED,
        )
        _annotate_residual_risk(profile)
        return profile

    # -- step 7: baseline + assembly --------------------------------------- #

    def _result(
        self,
        run_id: str,
        target: str,
        profile: RiskProfile,
        final_proposal: Proposal | None,
        assessment: YieldAssessment,
        review: AdversaryReview,
        unauditable: list[str],
        ledger: TokenLedger,
        finding_memory_records: list[str] | None = None,
        negotiation: list[RoundAdjudication] | None = None,
    ) -> RunResult:
        """Assemble the RunResult, running the Baseline control if configured."""
        baseline: BaselineAudit | None = None
        if self._baseline is not None:
            baseline = self._baseline.audit(_baseline_task(target))
            ledger.record("baseline", "single_pass", self._baseline.model)
        return RunResult(
            run_id=run_id,
            target=target,
            risk_profile=profile,
            final_proposal=final_proposal,
            final_assessment=assessment,
            final_review=review,
            unauditable_functions=unauditable,
            baseline=baseline,
            finding_memory_records=_dedup(finding_memory_records or []),
            negotiation=negotiation or [],
        )

    def _latest_patch_id(self) -> str | None:
        """Return the latest CodebaseMCP patch id visible before a proposal."""
        latest = self._latest_patch_result()
        return latest.patch_id if latest is not None else None

    def _latest_patch_result(self) -> ProposeResult | None:
        """Return CodebaseMCP's latest successful propose_patch result, if any."""
        latest = getattr(self._codebase, "latest_patch", None)
        if not callable(latest):
            return None
        try:
            result = latest()
        except Exception as exc:
            _log.warning("latest patch lookup failed", error=str(exc))
            return None
        if result is None:
            return None
        if isinstance(result, ProposeResult):
            return result
        try:
            return ProposeResult.model_validate(result)
        except Exception as exc:
            _log.warning("latest patch result invalid", error=str(exc))
            return None

    def _latest_patch_error(self) -> str | None:
        """Return CodebaseMCP's latest propose_patch error detail, if exposed."""
        latest_error = getattr(self._codebase, "latest_patch_error", None)
        if not callable(latest_error):
            return None
        try:
            error = latest_error()
        except Exception as exc:
            _log.warning("latest patch error lookup failed", error=str(exc))
            return None
        return str(error) if error else None

    def _attach_latest_patch_result(
        self, proposal: Proposal, latest_patch_before: str | None
    ) -> Proposal:
        """Copy a real CodebaseMCP patch id when the model omitted it."""
        if proposal.patch_id:
            return proposal
        latest = self._latest_patch_result()
        if latest is None:
            return proposal
        patch_id = latest.patch_id
        if not patch_id or patch_id == latest_patch_before:
            return proposal
        _log.warning(
            "arbitrator omitted patch_id; using latest CodebaseMCP staged patch",
            proposal_id=proposal.proposal_id,
            patch_id=patch_id,
        )
        return proposal.model_copy(
            update={
                "patch_id": patch_id,
                "staged_source_path": latest.staged_source_path,
                "staged_artifact": latest.staged_artifact,
                "contract_name": latest.contract_name,
                "artifact_path": latest.artifact_path,
                "original_target_path": latest.original_target_path,
            }
        )

    def _attach_staged_artifact(self, target: str, proposal: Proposal) -> Proposal:
        """Attach a compiled staged artifact target from CodebaseMCP when present."""
        if not _is_uploaded_target(target) or not proposal.patch_id:
            return proposal
        metadata_resolver = getattr(self._codebase, "staged_metadata", None)
        if callable(metadata_resolver):
            try:
                metadata = metadata_resolver(proposal.patch_id)
            except Exception as exc:
                _log.warning(
                    "staged metadata unavailable",
                    patch_id=proposal.patch_id,
                    error=str(exc),
                )
            else:
                if metadata is not None:
                    return proposal.model_copy(
                        update={
                            "staged_source_path": metadata.staged_source_path,
                            "staged_artifact": metadata.deploy_target,
                            "contract_name": metadata.contract_name,
                            "artifact_path": metadata.artifact_path,
                            "original_target_path": metadata.original_target_path,
                        }
                    )
        resolver = getattr(self._codebase, "staged_artifact", None)
        if not callable(resolver):
            return proposal
        try:
            artifact = resolver(proposal.patch_id)
        except Exception as exc:
            _log.warning(
                "staged artifact unavailable",
                patch_id=proposal.patch_id,
                error=str(exc),
            )
            return proposal
        if not artifact:
            return proposal
        return proposal.model_copy(update={"staged_artifact": artifact})


# --------------------------------------------------------------------------- #
# Context builders — §9 windowing: carry only the current proposal + the
# immediately preceding veto + active constraints, never the full transcript.
# --------------------------------------------------------------------------- #


def _initial_yield_task(target: str) -> str:
    return (
        f"Initial assessment: read {target} and judge whether it is ready to "
        f"ship against its business requirements. Cite specific functions."
    )


def _initial_adversary_task(target: str) -> str:
    if _is_uploaded_target(target):
        return (
            f"Initial scan of uploaded contract {target} (proposal id: original). "
            f"Use generic probes only: read_contract, list_functions, and "
            f"deploy_to_fork with [] so SimulationMCP can infer supported "
            f"constructor arguments from ABI. If inference reports "
            f"dynamic_verification_unavailable, continue with static audit and "
            f"say dynamic verification was unavailable. "
            f"Do not run demo scenarios such as nominal, fee_spike, or "
            f"high_congestion. Do not run exploit harnesses unless a named "
            f"harness clearly matches this contract's ABI. For source-only "
            f"findings say 'static finding only; no exploit trace available' "
            f"and do not claim exploit execution. Cite real SimulationMCP "
            f"trace_ids only for deployment or measured execution claims."
        )
    if "YieldVault" in target:
        return (
            f"Initial scan of {target} (proposal id: original). Read the contract, "
            f"deploy it to the fork, then test access control directly: measure "
            f"deposit with value, call setOperator from a non-owner account, and "
            f"call emergencyWithdraw from the reassigned operator. A privileged "
            f"call from a non-owner that does not revert is evidence. Cite every "
            f"SimulationMCP trace_id behind your verdict."
        )
    if "SubscriptionBilling" in target:
        return (
            f"Initial scan of {target} (proposal id: original). Deploy it to the "
            f"fork, run nominal plus fee_spike/high_congestion scenarios, and "
            f"inspect cancelSubscription for reentrancy risk. Valid scenario names "
            f"are: nominal, fee_spike, high_congestion. Cite every SimulationMCP "
            f"trace_id behind your verdict."
        )
    return (
        f"Initial scan of {target} (proposal id: original). Deploy it to the "
        f"fork and run relevant SimulationMCP scenarios. Valid scenario names "
        f"are: nominal, fee_spike, high_congestion. Cite every SimulationMCP "
        f"trace_id behind your verdict; do not use a scenario named baseline."
    )


def _initial_lessons_task(target: str) -> str:
    return (
        f"An audit of {target} is starting. Retrieve any prior incidents that "
        f"constrain how this contract class can safely be changed."
    )


def _constraint_lines(constraints: list[Constraint]) -> str:
    if not constraints:
        return "(none yet)"
    return "; ".join(c.constraint_text for c in constraints)


def _propose_task(
    target: str,
    run_id: str,
    n: int,
    last_veto: Veto | None,
    constraints: list[Constraint],
) -> str:
    veto_line = (
        f"Preceding veto: {last_veto.reason} (evidence: {last_veto.evidence})."
        if last_veto is not None
        else "No standing veto."
    )
    return (
        f"Round {n} for {target}. {veto_line} Active constraints: "
        f"{_constraint_lines(constraints)}. Stage patch v{n} via propose_patch "
        f"and return the Proposal (cite the motivating SimulationMCP trace_ids)."
    )


def _evaluate_task(proposal: Proposal) -> str:
    patch_path = _staged_patch_path(proposal)
    return (
        f"Evaluate staged patch {proposal.patch_id} ({proposal.summary}) against "
        f"the business requirements. Read the patch with "
        f"read_contract('{patch_path}') and list_functions('{patch_path}'). "
        f"Do not invent any other filename. Cite specific functions from that "
        f"staged patch."
    )


def _review_task(target: str, proposal: Proposal) -> str:
    patch_path = _staged_patch_path(proposal)
    deploy_target = _staged_deploy_target(proposal)
    if _is_uploaded_target(target):
        return (
            f"Review uploaded-contract staged patch {proposal.patch_id} "
            f"(proposal id {proposal.proposal_id}). Read the staged source at "
            f"{patch_path}; do not invent another filename. Use generic probes "
            f"only. Deploy the staged contract as {deploy_target} with [] so "
            f"SimulationMCP can infer "
            f"supported constructor arguments from ABI. If constructor inference "
            f"reports dynamic_verification_unavailable, continue static review and "
            f"report dynamic verification unavailable. Do not call measure_gas "
            f"unless deployment returned an address. Do not run demo scenarios or "
            f"unrelated exploit harnesses. "
            f"If dynamic exploit verification is unavailable, say 'static "
            f"finding only; no exploit trace available'. If the staged patch "
            f"cannot be deployed or simulated, report it as unverifiable with "
            f"the real degraded SimulationMCP trace_id."
        )
    return (
        f"Re-simulate {target} with staged patch {proposal.patch_id} (proposal "
        f"id {proposal.proposal_id}). The staged source is available to read at "
        f"{patch_path}; do not invent another filename. Valid scenario names are: "
        f"nominal, fee_spike, high_congestion. Veto or clear, citing "
        f"SimulationMCP trace_ids for every claim. Do not clear the proposal based "
        f"only on reset_fork or a failed deploy; if the staged patch cannot be "
        f"deployed or simulated, veto it as unverifiable."
    )


def _staged_patch_path(proposal: Proposal) -> str:
    """Return the CodebaseMCP read alias for a staged patch."""
    return f"contracts/staged_patches/{proposal.patch_id}.sol"


def _staged_deploy_target(proposal: Proposal) -> str:
    """Return the SimulationMCP deploy target for a staged patch."""
    return proposal.staged_artifact or _staged_patch_path(proposal)


def _is_uploaded_target(target: str) -> bool:
    """Return True for browser-uploaded audit workspace contracts."""
    return "/audit_workdir/" in target or "/uploads/" in target


def _is_patch_verification_tool_failure(review: AdversaryReview) -> bool:
    """Detect infra failures that should not become protocol veto rounds."""
    if not review.vetoed:
        return False
    if _is_dynamic_verification_unavailable(review):
        return False
    text = f"{review.reason} {review.evidence}".lower()
    markers = (
        "missing artifact",
        "build artifact",
        "cannot be deployed",
        "could not be deployed",
        "cannot deploy",
        "failed to deploy",
        "unverifiable",
        "no simulations can be run",
        "no simulation",
    )
    return any(marker in text for marker in markers)


def _is_dynamic_verification_unavailable(review: AdversaryReview) -> bool:
    """Detect unsupported generic dynamic verification, not protocol risk."""
    text = f"{review.reason} {review.evidence}".lower()
    markers = (
        "dynamic_verification_unavailable",
        "dynamic verification unavailable",
        "unsupported constructor",
        "constructor inference",
        "constructor argument",
        "arrays and structs are unsupported",
    )
    return any(marker in text for marker in markers)


def _memory_task(veto: Veto) -> str:
    return (
        f"A veto was raised: {veto.reason}. Retrieve prior constraints relevant "
        f"to this failure mode and reframe them as limitations the next patch "
        f"must satisfy."
    )


def _baseline_task(target: str) -> str:
    return (
        f"Single-pass audit of the original {target}: list the vulnerabilities "
        f"you can see from source alone and recommend one fix. No simulation."
    )


def _synthesis_task(
    run_id: str, outcome: Outcome, final_proposal: Proposal | None, iterations: int
) -> str:
    patch = final_proposal.patch_id if final_proposal else "none"
    return (
        f"Synthesize the RiskProfile for run {run_id}. Determined outcome: "
        f"{outcome.value}. Final staged patch: {patch}. Iterations: {iterations}. "
        f"Ground every residual-risk number in the cited SimulationMCP runs; do "
        f"not fabricate consensus or mitigations."
    )
