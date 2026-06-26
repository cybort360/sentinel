"""Regression tests for the deterministic demo checkpoint responder."""

from __future__ import annotations

import asyncio

from demo.run_demo import AutoApproveResponder

from sentinel.orchestrator.checkpoint import CheckpointDecision, DecisionPacket
from sentinel.orchestrator.schema import Outcome, RiskProfile


def test_auto_responder_acknowledges_incomplete_audit() -> None:
    packet = DecisionPacket(
        run_id="run-incomplete",
        target="sandbox/contracts/audit_workdir/Vulnerable.sol",
        gated_reasons=["Audit incomplete because a tool failed (§4 Rule 4)"],
        proposal=None,
        assessment=None,
        review=None,
        risk_profile=RiskProfile(
            run_id="run-incomplete",
            outcome=Outcome.TOOL_FAILURE,
            final_proposal=None,
            residual_risk_pct=0.0,
            residual_risk_description="patch staging failed",
            iterations=1,
            tokens_total=0,
            trace_ids=["sim-1"],
        ),
        memory_records_used=[],
        topic_tags=["uploaded-contract"],
    )

    response = asyncio.run(AutoApproveResponder().ask(packet))

    assert response.decision is CheckpointDecision.ACKNOWLEDGE_INCOMPLETE
    assert response.rationale is not None
    assert "incomplete audit" in response.rationale
