"""Rule-2 enforcement test (CLAUDE.md §6 / Golden Rule #2, architecture.md §7).

This is the test that must fail if the Human Checkpoint Gate is ever made fake.
It proves the gate **genuinely blocks**: the awaiting task stays pending and
nothing is approved or recorded until a human decision is delivered. If the gate
auto-approved (or returned a default), ``task.done()`` would be true before any
response is sent and ``test_checkpoint_blocks_until_human_responds`` would fail.

It needs no Anvil/sandbox, so it is intentionally left *unmarked* — it runs in
the default ``make check`` suite as a live guard, not only under
``make check-integration``.
"""

from __future__ import annotations

import asyncio
import io
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from rich.console import Console
from structlog.testing import capture_logs

from sentinel.orchestrator.checkpoint import (
    CheckpointDecision,
    CheckpointResponse,
    DecisionPacket,
    HumanCheckpoint,
    build_decision_packet,
    render_packet,
)
from sentinel.orchestrator.graph import RunResult
from sentinel.orchestrator.schema import (
    AdversaryReview,
    Outcome,
    Proposal,
    RiskProfile,
    Severity,
    YieldAssessment,
    YieldVerdict,
)

# --------------------------------------------------------------------------- #
# Fixtures / fakes
# --------------------------------------------------------------------------- #


class FakeWriter:
    """Records post-mortem writes; returns a fixed id."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def write_post_mortem(
        self,
        *,
        topic_tags: list[str],
        severity: Severity,
        description: str,
        lesson_text: str,
        source_run_id: str,
    ) -> str:
        self.calls.append(
            {
                "topic_tags": topic_tags,
                "severity": severity,
                "description": description,
                "lesson_text": lesson_text,
                "source_run_id": source_run_id,
            }
        )
        return "mem-pm-1"


class BlockingResponder:
    """Withholds the decision until the test explicitly delivers it."""

    def __init__(self) -> None:
        self.opened = asyncio.Event()
        self._future: asyncio.Future[CheckpointResponse] | None = None

    async def ask(self, packet: DecisionPacket) -> CheckpointResponse:
        self._future = asyncio.get_running_loop().create_future()
        self.opened.set()
        return await self._future

    def deliver(self, response: CheckpointResponse) -> None:
        assert self._future is not None and not self._future.done()
        self._future.set_result(response)


class ImmediateResponder:
    """Returns a preset response as soon as the gate asks."""

    def __init__(self, response: CheckpointResponse) -> None:
        self._response = response
        self.asked = 0

    async def ask(self, packet: DecisionPacket) -> CheckpointResponse:
        self.asked += 1
        return self._response


def _response(
    decision: CheckpointDecision,
    *,
    instruction: str | None = None,
    rationale: str | None = "looks safe to me",
) -> CheckpointResponse:
    return CheckpointResponse(
        decision=decision,
        rationale=rationale,
        instruction=instruction,
        responded_at=datetime.now(tz=UTC),
    )


def _packet(*, residual: float = 0.04, with_proposal: bool = True) -> DecisionPacket:
    proposal = (
        Proposal(
            proposal_id="p1",
            run_id="run-1",
            iteration=1,
            patch_id="patch-1",
            summary="add nonReentrant guard to cancelSubscription",
            trace_ids=["sim-1"],
        )
        if with_proposal
        else None
    )
    return DecisionPacket(
        run_id="run-1",
        target="SubscriptionBilling.sol",
        gated_reasons=["Applying a patch to the protocol (§7.1.2)"],
        proposal=proposal,
        assessment=YieldAssessment(
            run_id="run-1",
            iteration=1,
            verdict=YieldVerdict.ACCEPT,
            rationale="guard keeps processBilling bounded",
            referenced_functions=["cancelSubscription"],
        ),
        review=AdversaryReview(
            run_id="run-1",
            iteration=1,
            target_proposal_id="p1",
            vetoed=False,
            reason="reentrancy closed; revert rate 0% post-patch",
            evidence="get_revert_rate=0.0 under fee_spike",
            trace_ids=["sim-1", "sim-2"],
        ),
        risk_profile=RiskProfile(
            run_id="run-1",
            outcome=Outcome.CONSENSUS,
            final_proposal="patch-1",
            residual_risk_pct=residual,
            residual_risk_description="guard holds; 12.9% gas cost",
            mitigations_applied=["circuit-breaker on revert rate > 5%"],
            memory_records_used=["mem-batching-1"],
            iterations=1,
            tokens_total=1234,
            trace_ids=["sim-1", "sim-2"],
        ),
        memory_records_used=["mem-batching-1"],
        topic_tags=["reentrancy", "settlement"],
    )


# --------------------------------------------------------------------------- #
# THE Rule-2 test: the gate genuinely blocks until a human responds
# --------------------------------------------------------------------------- #


def test_checkpoint_blocks_until_human_responds() -> None:
    asyncio.run(_blocks_until_human_responds())


async def _blocks_until_human_responds() -> None:
    responder = BlockingResponder()
    writer = FakeWriter()
    gate = HumanCheckpoint(responder, writer)

    task = asyncio.create_task(gate.request(_packet()))

    # The gate must reach the point of asking the human...
    await asyncio.wait_for(responder.opened.wait(), timeout=1.0)
    # ...and then STOP. Give it room to (wrongly) resolve if it were going to.
    await asyncio.sleep(0.05)
    assert not task.done(), "Rule 2 violation: gate did not block for the human"
    assert writer.calls == [], "nothing may be recorded before a human decides"

    # Now the human approves — only now may the task complete.
    responder.deliver(_response(CheckpointDecision.APPROVE))
    outcome = await asyncio.wait_for(task, timeout=1.0)

    assert outcome.approved is True
    assert outcome.decision is CheckpointDecision.APPROVE
    assert outcome.post_mortem_id == "mem-pm-1"
    assert writer.calls and writer.calls[0]["source_run_id"] == "run-1"


# --------------------------------------------------------------------------- #
# Each decision is handled and recorded correctly (architecture.md §7.2)
# --------------------------------------------------------------------------- #


async def _decide(
    decision: CheckpointDecision,
    *,
    instruction: str | None = None,
    residual: float = 0.04,
) -> tuple[object, FakeWriter]:
    responder = ImmediateResponder(_response(decision, instruction=instruction))
    writer = FakeWriter()
    gate = HumanCheckpoint(responder, writer)
    outcome = await gate.request(_packet(residual=residual))
    return outcome, writer


def test_approve_authorizes_and_writes_post_mortem() -> None:
    outcome, writer = asyncio.run(_decide(CheckpointDecision.APPROVE))
    assert outcome.approved is True and outcome.escalated is False
    assert writer.calls[0]["severity"] is Severity.MEDIUM  # 4% residual
    assert "approve" in str(writer.calls[0]["lesson_text"])


def test_reject_records_but_does_not_authorize() -> None:
    outcome, writer = asyncio.run(_decide(CheckpointDecision.REJECT))
    assert outcome.approved is False and outcome.escalated is False
    assert outcome.post_mortem_id == "mem-pm-1"
    assert len(writer.calls) == 1


def test_escalate_halts_and_records_high_severity() -> None:
    outcome, writer = asyncio.run(_decide(CheckpointDecision.ESCALATE))
    assert outcome.approved is False and outcome.escalated is True
    assert writer.calls[0]["severity"] is Severity.HIGH  # escalation is high


def test_request_more_analysis_returns_instruction_without_post_mortem() -> None:
    outcome, writer = asyncio.run(
        _decide(
            CheckpointDecision.REQUEST_MORE_ANALYSIS,
            instruction="simulate a 5x fee spike against the guarded path",
        )
    )
    assert outcome.approved is False
    assert outcome.instruction == "simulate a 5x fee spike against the guarded path"
    assert outcome.post_mortem_id is None
    assert writer.calls == []  # run re-enters the War Room; not over yet


def test_request_more_analysis_requires_an_instruction() -> None:
    with pytest.raises(ValidationError):
        CheckpointResponse(
            decision=CheckpointDecision.REQUEST_MORE_ANALYSIS,
            responded_at=datetime.now(tz=UTC),
        )


# --------------------------------------------------------------------------- #
# §7.2.4 logging — the human response is appended to the run log
# --------------------------------------------------------------------------- #


def test_human_response_is_logged_to_run_log() -> None:
    # The decision is a structured §10 event (demo_tag + action), not a
    # hand-written narration string — the bracket is rendered, not logged.
    with capture_logs() as logs:
        asyncio.run(_decide(CheckpointDecision.APPROVE))
    decision_logs = [
        e
        for e in logs
        if e.get("demo_tag") == "HUMAN CHECKPOINT" and e.get("action") == "decision"
    ]
    assert decision_logs and decision_logs[0]["decision"] == "approve"
    assert decision_logs[0]["run_id"] == "run-1"
    assert any(e.get("action") == "gate_open" for e in logs)


# --------------------------------------------------------------------------- #
# §7.2 Decision Packet rendering and assembly
# --------------------------------------------------------------------------- #


def test_decision_packet_renders_positions_traces_and_risk() -> None:
    console = Console(file=io.StringIO(), record=True, width=160)
    render_packet(_packet(), console)
    out = console.export_text()
    assert "HUMAN CHECKPOINT" in out
    assert "patch-1" in out  # the proposal
    assert "sim-1" in out and "sim-2" in out  # SimulationMCP trace ids
    assert "4%" in out  # residual risk rendered
    assert "mem-batching-1" in out  # informing memory record


def test_build_decision_packet_and_gated_reasons() -> None:
    run = RunResult(
        run_id="run-9",
        target="SubscriptionBilling.sol",
        risk_profile=RiskProfile(
            run_id="run-9",
            outcome=Outcome.CONSTRAINTS_UNSATISFIED,
            final_proposal="patch-9",
            residual_risk_pct=0.2,
            residual_risk_description="atomic settlement vs latency unresolved",
            iterations=4,
            tokens_total=42,
            trace_ids=["sim-9"],
        ),
        final_proposal=Proposal(
            proposal_id="p9",
            run_id="run-9",
            iteration=4,
            patch_id="patch-9",
            summary="batch settlement",
            trace_ids=["sim-9"],
        ),
        final_assessment=YieldAssessment(
            run_id="run-9",
            iteration=4,
            verdict=YieldVerdict.REVISE,
            rationale="latency budget tight",
            referenced_functions=["processBilling"],
        ),
        final_review=AdversaryReview(
            run_id="run-9",
            iteration=4,
            target_proposal_id="p9",
            vetoed=True,
            severity=Severity.HIGH,
            reason="finality delay breaches atomic settlement",
            evidence="finality 3 blocks under congestion",
            trace_ids=["sim-9"],
        ),
    )
    packet = build_decision_packet(run, topic_tags=["batching", "settlement-latency"])
    assert packet.proposal is not None and packet.proposal.patch_id == "patch-9"
    reasons = " | ".join(packet.gated_reasons)
    assert "patch" in reasons  # §7.1.2
    assert "residual risk" in reasons  # §7.1.1
    assert "unresolved Adversary veto" in reasons  # §7.1.3
