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

from sentinel.observability.trace_logger import DemoTag, emit, get_logger
from sentinel.orchestrator.schema import (
    AdversaryReview,
    BaselineAudit,
    Constraint,
    LessonsContext,
    Outcome,
    Proposal,
    RiskProfile,
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


def _annotate_residual_risk(profile: RiskProfile) -> None:
    """Emit the §10 ``[ANNOTATED RESIDUAL RISK]`` line from the RiskProfile.

    Rendered from the structured profile (Rule 1: cites a real trace id), not
    written as separate narration.
    """
    emit(
        _log,
        DemoTag.ANNOTATED_RESIDUAL_RISK,
        f"{profile.residual_risk_pct:.0%} — {profile.residual_risk_description}",
        run_id=profile.run_id,
        agent="arbitrator",
        action="risk_profile",
        trace_id=profile.trace_ids[0],
        residual_risk_pct=profile.residual_risk_pct,
        outcome=profile.outcome.value,
    )


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
            proposal = self._arbitrator.propose(
                _propose_task(target, run_id, n, last_veto, constraints)
            )
            ledger.record("arbitrator", f"propose_v{n}", self._arbitrator.model)
            trace_ids += proposal.trace_ids
            final_proposal = proposal
            _emit_proposal(proposal, run_id, n)

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

            if assessment.verdict is YieldVerdict.ACCEPT and not review.vetoed:
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
            residual_risk_description=draft.residual_risk_description,
            mitigations_applied=draft.mitigations_applied,
            memory_records_used=_dedup(loop.memory_ids),
            iterations=loop.iterations,
            tokens_total=ledger.total,
            trace_ids=_dedup(loop.trace_ids),
        )
        _annotate_residual_risk(profile)
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
        )


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
    return (
        f"Initial scan of {target} (proposal id: original). Deploy it to the "
        f"fork and run the baseline scenario sweep; cite every SimulationMCP "
        f"trace_id behind your verdict."
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
    return (
        f"Evaluate staged patch {proposal.patch_id} ({proposal.summary}) against "
        f"the business requirements. Cite specific functions."
    )


def _review_task(target: str, proposal: Proposal) -> str:
    return (
        f"Re-simulate {target} with staged patch {proposal.patch_id} (proposal "
        f"id {proposal.proposal_id}). Veto or clear, citing SimulationMCP "
        f"trace_ids for every claim."
    )


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
