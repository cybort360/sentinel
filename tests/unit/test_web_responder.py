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
from sentinel.orchestrator.schema import Outcome, RiskProfile
from sentinel.web import TraceBus, WebResponder
from sentinel.web.responder import CheckpointNotPendingError


def _packet(run_id: str = "run-web-1") -> DecisionPacket:
    return DecisionPacket(
        run_id=run_id,
        target="SubscriptionBilling.sol",
        gated_reasons=["Proceeding past an unresolved Adversary veto (§7.1.3)"],
        proposal=None,
        assessment=None,
        review=None,
        risk_profile=RiskProfile(
            run_id=run_id,
            outcome=Outcome.CONSTRAINTS_UNSATISFIED,
            residual_risk_pct=0.04,
            residual_risk_description="batching vs latency tension; 0.04% residual",
            iterations=2,
            tokens_total=0,
            trace_ids=["sim-web-1"],
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
