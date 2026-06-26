"""Human Checkpoint Gate — a real, blocking control-flow component (§7).

This is the Track 4 / project-principle load-bearing piece (CLAUDE.md Golden
Rule #2): when a gated decision is reached the run **genuinely halts** and waits
for a human, never auto-approving. The blocking happens by awaiting a
:class:`HumanResponder`; the CLI responder runs the actual ``input()`` in a
thread executor so the await suspends the run until a person answers. There is no
default, timeout-approve, or fallback "yes" path anywhere in this module — the
only way ``approved`` becomes true is a human choosing ``approve``.

Flow (architecture.md §7.2): present a :class:`DecisionPacket` → block on one of
**approve / reject / request-more-analysis / escalate** → append the response
(decision, timestamp, rationale) to the run log and, on a terminal decision,
fold it into the post-mortem written to ``MemoryMCP``. ``request_more_analysis``
re-enters the War Room (no post-mortem yet — the run is not over); the other
three end the run. The post-mortem write here is the §7.1.4 "lightweight
confirmation": the same human interaction authorises it, so it does not open a
second nested prompt.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, model_validator
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from sentinel.memory.schema import MemoryRecord
from sentinel.observability.trace_logger import DemoTag, emit, get_logger
from sentinel.orchestrator.schema import (
    AdversaryReview,
    DynamicVerificationStatus,
    Outcome,
    Proposal,
    RiskProfile,
    RoundAdjudication,
    Severity,
    YieldAssessment,
)

if TYPE_CHECKING:
    from sentinel.mcp_servers.codebase_mcp.results import ApplyResult
    from sentinel.memory.store import MemoryStore
    from sentinel.orchestrator.graph import RunResult

_log = get_logger("checkpoint")


def _now() -> datetime:
    """Return an aware UTC timestamp for the checkpoint record."""
    return datetime.now(tz=UTC)


class CheckpointDecision(StrEnum):
    """The human responses at a gated decision (architecture.md §7.2)."""

    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_MORE_ANALYSIS = "request_more_analysis"
    ESCALATE = "escalate"
    ACKNOWLEDGE_INCOMPLETE = "acknowledge_incomplete"


class CheckpointResponse(BaseModel):
    """A human's response to a Decision Packet, logged verbatim (§7.2.4)."""

    decision: CheckpointDecision
    rationale: str | None = None
    instruction: str | None = None
    responded_at: datetime

    @model_validator(mode="after")
    def _more_analysis_needs_instruction(self) -> CheckpointResponse:
        """``request_more_analysis`` must carry the instruction to re-run with."""
        if (
            self.decision is CheckpointDecision.REQUEST_MORE_ANALYSIS
            and not (self.instruction or "").strip()
        ):
            raise ValueError(
                "request_more_analysis requires a non-empty instruction for the "
                "War Room to re-enter with (architecture.md §7.2)"
            )
        return self


class DecisionPacket(BaseModel):
    """What the human sees at the gate (architecture.md §7.2).

    Carries the proposal under consideration, the closing Yield/Adversary
    positions (with their ``SimulationMCP`` trace ids), the ``RiskProfile``, the
    memory records that informed the proposal, and the per-round negotiation
    transcript (how each Yield/Adversary conflict was resolved — §4.3).
    """

    run_id: str
    target: str
    gated_reasons: list[str]
    proposal: Proposal | None
    assessment: YieldAssessment | None
    review: AdversaryReview | None
    risk_profile: RiskProfile
    memory_records_used: list[str]
    topic_tags: list[str]
    negotiation: list[RoundAdjudication] = []


class CheckpointOutcome(BaseModel):
    """The result of a gate: the decision plus what the orchestrator should do."""

    decision: CheckpointDecision
    response: CheckpointResponse
    approved: bool
    escalated: bool
    instruction: str | None = None
    post_mortem_id: str | None = None


class HumanResponder(Protocol):
    """Source of a human decision. The CLI impl blocks on real input (§7.3)."""

    async def ask(self, packet: DecisionPacket) -> CheckpointResponse:
        """Present ``packet`` and block until the human responds."""
        ...


class PatchApplier(Protocol):
    """The CodebaseMCP surface needed to apply an approved patch (§5.1, §8 step 6)."""

    def apply_patch(self, patch_id: str) -> ApplyResult:
        """Apply a staged patch to the canonical tree, returning the result."""
        ...


class PostMortemWriter(Protocol):
    """Sink for the checkpoint post-mortem (architecture.md §7.2.4)."""

    def write_post_mortem(
        self,
        *,
        topic_tags: list[str],
        severity: Severity,
        description: str,
        lesson_text: str,
        source_run_id: str,
    ) -> str:
        """Persist a post-mortem record and return its id."""
        ...


def gated_reasons(run_result: RunResult) -> list[str]:
    """List which §7.1 critical-decision conditions apply to this run."""
    profile = run_result.risk_profile
    reasons: list[str] = []
    if (
        run_result.final_proposal is not None
        and run_result.final_proposal.patch_id is not None
        and profile.outcome is not Outcome.TOOL_FAILURE
    ):
        reasons.append("Applying a patch to the protocol (§7.1.2)")
    if profile.residual_risk_pct > 0.0:
        reasons.append(
            f"Deployment with non-zero residual risk "
            f"({profile.residual_risk_pct:.0%}) (§7.1.1)"
        )
    if profile.outcome is Outcome.CONSTRAINTS_UNSATISFIED:
        reasons.append("Proceeding past an unresolved Adversary veto (§7.1.3)")
    if profile.outcome is Outcome.TOOL_FAILURE:
        reasons.append("Audit incomplete because a tool failed (§4 Rule 4)")
    if profile.outcome is Outcome.DYNAMIC_VERIFICATION_UNAVAILABLE:
        reasons.append(
            "Dynamic verification unavailable; static audit only (§4 Rule 4)"
        )
    if not reasons:
        reasons.append("Deployment authorization (§7.1.1)")
    return reasons


def _unverified_dynamic(profile: RiskProfile) -> bool:
    """Return whether dynamic verification did not produce a risk percentage."""
    return (
        profile.outcome is Outcome.DYNAMIC_VERIFICATION_UNAVAILABLE
        or profile.dynamic_verification_status
        in (
            DynamicVerificationStatus.UNAVAILABLE,
            DynamicVerificationStatus.INCOMPLETE,
        )
    )


def _risk_label(profile: RiskProfile) -> str:
    """Return the human-facing residual risk label."""
    if _unverified_dynamic(profile):
        return "Unverified"
    return f"{profile.residual_risk_pct:.0%}"


def build_decision_packet(
    run_result: RunResult, *, topic_tags: list[str] | None = None
) -> DecisionPacket:
    """Assemble the §7.2 Decision Packet from a completed run."""
    return DecisionPacket(
        run_id=run_result.run_id,
        target=run_result.target,
        gated_reasons=gated_reasons(run_result),
        proposal=run_result.final_proposal,
        assessment=run_result.final_assessment,
        review=run_result.final_review,
        risk_profile=run_result.risk_profile,
        memory_records_used=run_result.risk_profile.memory_records_used,
        topic_tags=topic_tags or ["audit"],
        negotiation=run_result.negotiation,
    )


def render_packet(packet: DecisionPacket, console: Console) -> None:
    """Render the Decision Packet to ``console`` (architecture.md §7.2/§7.3)."""
    console.print(
        Panel(
            f"run [bold]{packet.run_id}[/] — target [bold]{packet.target}[/]\n"
            f"Gated because: " + "; ".join(packet.gated_reasons),
            title="[HUMAN CHECKPOINT]",
            border_style="yellow",
        )
    )
    proposal = packet.proposal
    console.print(
        Panel(
            f"patch [bold]{proposal.patch_id}[/]: {proposal.summary}"
            if proposal is not None
            else "No patch staged (deployment of original).",
            title="Proposal under consideration",
        )
    )
    console.print(_positions_table(packet))
    console.print(_risk_panel(packet.risk_profile))
    mem = ", ".join(packet.memory_records_used) or "(none)"
    console.print(
        Panel(
            f"records: {mem}\ntopics: {', '.join(packet.topic_tags)}",
            title="Memory that informed this",
        )
    )


def _positions_table(packet: DecisionPacket) -> Table:
    """Build the Yield/Adversary positions table (with trace ids)."""
    table = Table(title="War Room positions", show_lines=True)
    table.add_column("Agent")
    table.add_column("Position")
    table.add_column("SimulationMCP trace_ids")
    if packet.assessment is not None:
        table.add_row(
            "Yield",
            f"{packet.assessment.verdict.value}: {packet.assessment.rationale}",
            "(reads source; no sim claims)",
        )
    if packet.review is not None:
        verdict = "VETO" if packet.review.vetoed else "clear"
        table.add_row(
            "Adversary",
            f"{verdict}: {packet.review.reason} — {packet.review.evidence}",
            ", ".join(packet.review.trace_ids),
        )
    return table


def _risk_panel(profile: RiskProfile) -> Panel:
    """Build the RiskProfile panel (residual risk, mitigations, traces)."""
    mitigations = (
        "\n".join(f"  • {m}" for m in profile.mitigations_applied) or "  (none)"
    )
    label = _risk_label(profile)
    return Panel(
        f"outcome: [bold]{profile.outcome.value}[/]\n"
        f"residual risk: [bold]{label}[/] - "
        f"{profile.residual_risk_description}\n"
        f"mitigations applied:\n{mitigations}\n"
        f"trace_ids: {', '.join(profile.trace_ids)}",
        title="RiskProfile",
        border_style=(
            "red"
            if profile.residual_risk_pct > 0 or _unverified_dynamic(profile)
            else "green"
        ),
    )


_CHOICE_TO_DECISION = {
    "approve": CheckpointDecision.APPROVE,
    "reject": CheckpointDecision.REJECT,
    "more": CheckpointDecision.REQUEST_MORE_ANALYSIS,
    "escalate": CheckpointDecision.ESCALATE,
    "acknowledge": CheckpointDecision.ACKNOWLEDGE_INCOMPLETE,
}


def packet_allows_approval(packet: DecisionPacket) -> bool:
    """Return whether this packet has a staged proposal a human may approve."""
    return (
        packet.proposal is not None
        and bool(packet.proposal.patch_id)
        and packet.risk_profile.outcome is not Outcome.TOOL_FAILURE
        and packet.risk_profile.dynamic_verification_status
        is not DynamicVerificationStatus.FAILED
    )


def packet_is_incomplete_audit(packet: DecisionPacket) -> bool:
    """Return whether the gate represents an incomplete audit, not approval."""
    has_patch = packet.proposal is not None and bool(packet.proposal.patch_id)
    return packet.risk_profile.outcome is Outcome.TOOL_FAILURE or not has_patch


def packet_allows_incomplete_acknowledgement(packet: DecisionPacket) -> bool:
    """Return whether acknowledgement is a valid terminal response."""
    return (
        packet_is_incomplete_audit(packet)
        or packet.risk_profile.outcome is Outcome.DYNAMIC_VERIFICATION_UNAVAILABLE
    )


class ConsoleResponder:
    """Rich CLI responder: renders the packet and blocks on real input (§7.3)."""

    def __init__(self, console: Console | None = None) -> None:
        """Create the responder, optionally with a pre-configured console."""
        self._console = console or Console()

    async def ask(self, packet: DecisionPacket) -> CheckpointResponse:
        """Render the packet and await the human's choice (genuinely blocking).

        The blocking ``input()`` runs in the default executor, so awaiting this
        suspends the run — it does not busy-wait and does not auto-answer.
        """
        render_packet(packet, self._console)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._blocking_prompt, packet)

    def _blocking_prompt(self, packet: DecisionPacket) -> CheckpointResponse:
        """Prompt for a decision on the calling thread (blocks until answered)."""
        choices = list(_CHOICE_TO_DECISION)
        if packet_is_incomplete_audit(packet):
            choices = ["reject", "escalate", "acknowledge"]
        elif not packet_allows_approval(packet):
            choices.remove("approve")
        choice = Prompt.ask(
            "Decision",
            choices=choices,
            console=self._console,
        )
        decision = _CHOICE_TO_DECISION[choice]
        instruction: str | None = None
        if decision is CheckpointDecision.REQUEST_MORE_ANALYSIS:
            instruction = Prompt.ask(
                "Instruction for the War Room", console=self._console
            )
        rationale = Prompt.ask(
            "Rationale (optional)", default="", console=self._console
        )
        return CheckpointResponse(
            decision=decision,
            rationale=rationale or None,
            instruction=instruction,
            responded_at=_now(),
        )


class HumanCheckpoint:
    """The blocking gate (architecture.md §7, CLAUDE.md Golden Rule #2)."""

    def __init__(
        self,
        responder: HumanResponder,
        writer: PostMortemWriter | None = None,
    ) -> None:
        """Wire the gate to its responder and (optional) post-mortem sink.

        Args:
            responder: Where the human decision comes from. The run blocks on
                this; there is no auto-approve path.
            writer: Sink for the §7.2.4 post-mortem on a terminal decision.
        """
        self._responder = responder
        self._writer = writer

    async def request(self, packet: DecisionPacket) -> CheckpointOutcome:
        """Halt at the gate, block for the human, then log and record (§7.2).

        Returns:
            The :class:`CheckpointOutcome`. ``approved`` is true only when the
            human explicitly approved — never by default.
        """
        emit(
            _log,
            DemoTag.HUMAN_CHECKPOINT,
            "gate open — awaiting human",
            run_id=packet.run_id,
            agent="human",
            action="gate_open",
            target=packet.target,
            gated_reasons=packet.gated_reasons,
        )
        response = await self._responder.ask(packet)
        invalid_approval = (
            response.decision is CheckpointDecision.APPROVE
            and not packet_allows_approval(packet)
        )
        if invalid_approval:
            raise ValueError("approve requires a staged patch_id in the checkpoint")
        invalid_acknowledgement = (
            response.decision is CheckpointDecision.ACKNOWLEDGE_INCOMPLETE
            and not packet_allows_incomplete_acknowledgement(packet)
        )
        if invalid_acknowledgement:
            raise ValueError(
                "acknowledge_incomplete requires an incomplete or unverified audit"
            )
        emit(
            _log,
            DemoTag.HUMAN_CHECKPOINT,
            f"human chose to {response.decision.value}",
            run_id=packet.run_id,
            agent="human",
            action="decision",
            decision=response.decision.value,
            rationale=response.rationale,
            instruction=response.instruction,
            responded_at=response.responded_at.isoformat(),
        )
        return self._handle(packet, response)

    def _handle(
        self, packet: DecisionPacket, response: CheckpointResponse
    ) -> CheckpointOutcome:
        """Map a response to an outcome, writing a post-mortem if terminal."""
        if response.decision is CheckpointDecision.REQUEST_MORE_ANALYSIS:
            return CheckpointOutcome(
                decision=response.decision,
                response=response,
                approved=False,
                escalated=False,
                instruction=response.instruction,
            )
        post_mortem_id = self._write_post_mortem(packet, response)
        return CheckpointOutcome(
            decision=response.decision,
            response=response,
            approved=response.decision is CheckpointDecision.APPROVE,
            escalated=response.decision is CheckpointDecision.ESCALATE,
            post_mortem_id=post_mortem_id,
        )

    def _write_post_mortem(
        self, packet: DecisionPacket, response: CheckpointResponse
    ) -> str | None:
        """Fold the human decision into a MemoryMCP post-mortem (§7.2.4)."""
        if self._writer is None:
            return None
        risk = _risk_label(packet.risk_profile)
        post_mortem_id = self._writer.write_post_mortem(
            topic_tags=packet.topic_tags,
            severity=_post_mortem_severity(packet, response),
            description=(
                f"Run {packet.run_id} on {packet.target}: outcome "
                f"{packet.risk_profile.outcome.value}, residual risk {risk}, "
                f"human decision {response.decision.value}."
            ),
            lesson_text=_post_mortem_lesson(packet, response),
            source_run_id=packet.run_id,
        )
        emit(
            _log,
            DemoTag.HUMAN_CHECKPOINT,
            "post-mortem recorded to MemoryMCP",
            run_id=packet.run_id,
            agent="lessons",
            action="post_mortem",
            post_mortem_id=post_mortem_id,
        )
        return post_mortem_id


def _post_mortem_severity(
    packet: DecisionPacket, response: CheckpointResponse
) -> Severity:
    """Grade the post-mortem: escalations and high residual risk are high."""
    if response.decision is CheckpointDecision.ESCALATE:
        return Severity.HIGH
    if _unverified_dynamic(packet.risk_profile):
        return Severity.MEDIUM
    pct = packet.risk_profile.residual_risk_pct
    if pct > 0.1:
        return Severity.HIGH
    if pct > 0.0:
        return Severity.MEDIUM
    return Severity.LOW


def _post_mortem_lesson(packet: DecisionPacket, response: CheckpointResponse) -> str:
    """Frame the human decision as a standing constraint, not a fix (§4.4)."""
    risk = _risk_label(packet.risk_profile)
    rationale = f" Rationale: {response.rationale}." if response.rationale else ""
    return (
        f"A human chose to {response.decision.value} this audit at {risk} "
        f"residual risk.{rationale} Standing constraint: future audits touching "
        f"{', '.join(packet.topic_tags)} must re-surface this decision for human "
        f"sign-off rather than assume it was permanently resolved."
    )


class MemoryStorePostMortemWriter:
    """Adapts :class:`MemoryStore` to the :class:`PostMortemWriter` protocol."""

    def __init__(self, store: MemoryStore) -> None:
        """Wrap a memory store for field-based post-mortem writes."""
        self._store = store

    def write_post_mortem(
        self,
        *,
        topic_tags: list[str],
        severity: Severity,
        description: str,
        lesson_text: str,
        source_run_id: str,
    ) -> str:
        """Construct and persist the post-mortem record, returning its id."""
        record = self._store.write_memory(
            MemoryRecord(
                topic_tags=topic_tags,
                severity=severity,
                description=description,
                lesson_text=lesson_text,
                source_run_id=source_run_id,
            )
        )
        return record.id


def apply_approved_patch(
    outcome: CheckpointOutcome,
    *,
    codebase: PatchApplier,
    patch_id: str | None,
) -> ApplyResult | None:
    """Apply a staged patch to the canonical tree, gated on human approval.

    This is the §8 step-6 action and the ONLY place a patch reaches the canonical
    file. It is gated on ``outcome.approved`` — a reject / escalate /
    request-more-analysis decision never applies, and there is no auto-apply path
    (Golden Rule #2 extends past the gate, not just to it). On a CodebaseMCP
    failure it logs a structured ``[DEGRADED]`` entry and returns ``None`` rather
    than crashing the run (Rule 4).

    Args:
        outcome: The human checkpoint outcome.
        codebase: The CodebaseMCP apply surface (``apply_patch``).
        patch_id: The staged patch to apply (the proposal's ``patch_id``), or
            ``None`` if the run staged no patch.

    Returns:
        The :class:`ApplyResult` on a successful approved apply, otherwise
        ``None`` (not approved, no staged patch, or a degraded apply).
    """
    if not outcome.approved or not patch_id:
        return None
    try:
        result = codebase.apply_patch(patch_id)
    except Exception as exc:  # noqa: BLE001 — degrade, never crash the run (Rule 4)
        _log.error(
            "[DEGRADED] apply_patch failed after approval",
            patch_id=patch_id,
            error=str(exc),
        )
        return None
    short = (result.commit or "?")[:8]
    emit(
        _log,
        DemoTag.SYSTEM_DECISION,
        f"patch {patch_id} applied to canonical tree (commit {short})",
        agent="orchestrator",
        action="apply_patch",
        patch_id=patch_id,
        commit=result.commit,
    )
    return result
