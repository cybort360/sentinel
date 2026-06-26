"""Rule-2 guard for the browser checkpoint (architecture.md §18, Golden Rule #2).

The web mirror of ``test_checkpoint_blocks``: it proves the :class:`WebResponder`
genuinely blocks the run until a browser submits a decision, and that the real
:class:`HumanCheckpoint` gate built on it never auto-approves. If ``ask`` returned
a default (or ``resolve`` were bypassable), ``task.done()`` would be true before
any decision is delivered and these would fail.
"""

from __future__ import annotations

import asyncio

import pytest

from sentinel.orchestrator.checkpoint import (
    CheckpointDecision,
    DecisionPacket,
    HumanCheckpoint,
)
from sentinel.orchestrator.schema import (
    AgentRole,
    DynamicVerificationStatus,
    Outcome,
    Proposal,
    RiskProfile,
)
from sentinel.web import TraceBus, WebResponder
from sentinel.web.responder import CheckpointNotPendingError


def _packet(
    run_id: str = "run-web-1",
    *,
    has_patch: bool = True,
    outcome: Outcome = Outcome.CONSTRAINTS_UNSATISFIED,
    dynamic_status: DynamicVerificationStatus = DynamicVerificationStatus.PASSED,
) -> DecisionPacket:
    proposal = (
        Proposal(
            proposal_id="p-web",
            run_id=run_id,
            iteration=1,
            patch_id="patch-web",
            proposed_by=AgentRole.ARBITRATOR,
            summary="stage a patch",
            trace_ids=["sim-web-1"],
        )
        if has_patch
        else None
    )
    return DecisionPacket(
        run_id=run_id,
        target="SubscriptionBilling.sol",
        gated_reasons=["Proceeding past an unresolved Adversary veto (§7.1.3)"],
        proposal=proposal,
        assessment=None,
        review=None,
        risk_profile=RiskProfile(
            run_id=run_id,
            outcome=outcome,
            residual_risk_pct=0.04,
            residual_risk_description="batching vs latency tension; 0.04% residual",
            iterations=2,
            tokens_total=0,
            trace_ids=["sim-web-1"],
            dynamic_verification_status=dynamic_status,
        ),
        memory_records_used=["mem-1"],
        topic_tags=["batching"],
    )


def test_web_responder_blocks_until_browser_decides() -> None:
    asyncio.run(_blocks_until_browser_decides())


async def _blocks_until_browser_decides() -> None:
    bus = TraceBus()
    responder = WebResponder(bus)
    gate = HumanCheckpoint(responder)

    task = asyncio.create_task(gate.request(_packet()))

    # Let the gate reach the point of awaiting the human, then STOP there.
    for _ in range(50):
        if responder.is_pending("run-web-1"):
            break
        await asyncio.sleep(0.005)
    await asyncio.sleep(0.05)
    assert responder.is_pending("run-web-1"), "gate never opened"
    assert not task.done(), "Rule 2 violation: gate did not block for the human"

    # Only a real decision (a POST -> resolve) may complete it.
    responder.resolve("run-web-1", CheckpointDecision.APPROVE, rationale="ship it")
    outcome = await asyncio.wait_for(task, timeout=1.0)

    assert outcome.approved is True
    assert outcome.decision is CheckpointDecision.APPROVE
    assert not responder.is_pending("run-web-1")


def test_resolve_without_open_gate_raises() -> None:
    bus = TraceBus()
    responder = WebResponder(bus)
    with pytest.raises(CheckpointNotPendingError):
        responder.resolve("nope", CheckpointDecision.APPROVE)


def test_approve_without_patch_is_rejected() -> None:
    asyncio.run(_approve_without_patch_is_rejected())


async def _approve_without_patch_is_rejected() -> None:
    bus = TraceBus()
    responder = WebResponder(bus)
    task = asyncio.create_task(responder.ask(_packet("run-no-patch", has_patch=False)))
    for _ in range(50):
        if responder.is_pending("run-no-patch"):
            break
        await asyncio.sleep(0.005)

    with pytest.raises(ValueError, match="staged patch_id"):
        responder.resolve("run-no-patch", CheckpointDecision.APPROVE)

    responder.resolve("run-no-patch", CheckpointDecision.REJECT)
    response = await asyncio.wait_for(task, timeout=1.0)
    assert response.decision is CheckpointDecision.REJECT


def test_approve_on_tool_failure_is_rejected() -> None:
    asyncio.run(_approve_on_tool_failure_is_rejected())


async def _approve_on_tool_failure_is_rejected() -> None:
    bus = TraceBus()
    responder = WebResponder(bus)
    task = asyncio.create_task(
        responder.ask(_packet("run-tool-failure", outcome=Outcome.TOOL_FAILURE))
    )
    for _ in range(50):
        if responder.is_pending("run-tool-failure"):
            break
        await asyncio.sleep(0.005)

    with pytest.raises(ValueError, match="staged patch_id"):
        responder.resolve("run-tool-failure", CheckpointDecision.APPROVE)

    responder.resolve("run-tool-failure", CheckpointDecision.ESCALATE)
    response = await asyncio.wait_for(task, timeout=1.0)
    assert response.decision is CheckpointDecision.ESCALATE


def test_acknowledge_incomplete_audit_releases_web_gate() -> None:
    asyncio.run(_acknowledge_incomplete_audit_releases_web_gate())


async def _acknowledge_incomplete_audit_releases_web_gate() -> None:
    bus = TraceBus()
    responder = WebResponder(bus)
    task = asyncio.create_task(
        responder.ask(
            _packet(
                "run-tool-failure-ack",
                has_patch=False,
                outcome=Outcome.TOOL_FAILURE,
            )
        )
    )
    for _ in range(50):
        if responder.is_pending("run-tool-failure-ack"):
            break
        await asyncio.sleep(0.005)

    responder.resolve(
        "run-tool-failure-ack",
        CheckpointDecision.ACKNOWLEDGE_INCOMPLETE,
        rationale="patch staging failed",
    )
    response = await asyncio.wait_for(task, timeout=1.0)

    assert response.decision is CheckpointDecision.ACKNOWLEDGE_INCOMPLETE
    assert response.rationale == "patch staging failed"
    assert not responder.is_pending("run-tool-failure-ack")


def test_approve_on_dynamic_verification_unavailable_is_allowed() -> None:
    asyncio.run(_approve_on_dynamic_verification_unavailable_is_allowed())


async def _approve_on_dynamic_verification_unavailable_is_allowed() -> None:
    bus = TraceBus()
    responder = WebResponder(bus)
    task = asyncio.create_task(
        responder.ask(
            _packet(
                "run-dynamic-unavailable",
                outcome=Outcome.DYNAMIC_VERIFICATION_UNAVAILABLE,
                dynamic_status=DynamicVerificationStatus.UNAVAILABLE,
            )
        )
    )
    for _ in range(50):
        if responder.is_pending("run-dynamic-unavailable"):
            break
        await asyncio.sleep(0.005)

    responder.resolve("run-dynamic-unavailable", CheckpointDecision.APPROVE)
    response = await asyncio.wait_for(task, timeout=1.0)
    assert response.decision is CheckpointDecision.APPROVE


def test_reject_is_not_approval() -> None:
    outcome = asyncio.run(_decide(CheckpointDecision.REJECT))
    assert outcome.approved is False
    assert outcome.decision is CheckpointDecision.REJECT


async def _decide(decision: CheckpointDecision) -> object:
    bus = TraceBus()
    responder = WebResponder(bus)
    gate = HumanCheckpoint(responder)
    task = asyncio.create_task(gate.request(_packet("run-web-2")))
    for _ in range(50):
        if responder.is_pending("run-web-2"):
            break
        await asyncio.sleep(0.005)
    responder.resolve("run-web-2", decision)
    return await asyncio.wait_for(task, timeout=1.0)
