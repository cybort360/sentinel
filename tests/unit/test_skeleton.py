"""Smoke tests for the SENTINEL skeleton: imports, schemas, and Golden Rule #1.

This is a baseline sanity check, not the full Rule-1 enforcement suite — that
lives in ``tests/unit/test_no_unsourced_claims.py`` once the agents exist.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sentinel.memory.schema import MemoryRecord, MemoryStatus
from sentinel.observability.trace_logger import get_logger
from sentinel.orchestrator.schema import (
    AgentRole,
    Outcome,
    Proposal,
    RiskProfile,
    Severity,
    Veto,
)


def test_proposal_rejects_empty_trace_ids() -> None:
    with pytest.raises(ValidationError):
        Proposal(
            proposal_id="p1",
            run_id="r1",
            iteration=0,
            summary="add reentrancy guard",
            trace_ids=[],
        )


def test_veto_round_trips_with_trace() -> None:
    veto = Veto(
        veto_id="v1",
        run_id="r1",
        iteration=1,
        target_proposal_id="p1",
        issued_by=AgentRole.ADVERSARY,
        severity=Severity.HIGH,
        reason="gas regression breaches micro-settlement baseline",
        evidence="+15% gas vs baseline",
        trace_ids=["simulationmcp-123"],
    )
    assert veto.issued_by is AgentRole.ADVERSARY
    assert veto.trace_ids == ["simulationmcp-123"]


def test_risk_profile_clamps_residual_risk() -> None:
    with pytest.raises(ValidationError):
        RiskProfile(
            run_id="r1",
            outcome=Outcome.CONSENSUS,
            residual_risk_pct=1.5,  # out of [0, 1]
            residual_risk_description="impossible",
            iterations=0,
            tokens_total=0,
            trace_ids=["simulationmcp-1"],
        )


def test_memory_record_requires_topic_tags() -> None:
    with pytest.raises(ValidationError):
        MemoryRecord(
            topic_tags=[],
            severity=Severity.LOW,
            description="tried something",
            lesson_text="introduced a new constraint",
            source_run_id="r1",
        )


def test_memory_record_defaults_are_decay_eligible() -> None:
    rec = MemoryRecord(
        topic_tags=["batching", "settlement-latency"],
        severity=Severity.MEDIUM,
        description="batching pattern fixed fee spikes",
        lesson_text="batching introduces silent finality delay",
        source_run_id="r1",
    )
    assert rec.status is MemoryStatus.ACTIVE
    assert rec.id  # auto-assigned uuid
    assert rec.created_at is not None  # always populated for decay scoring


def test_logger_emits() -> None:
    log = get_logger("test")
    log.info("skeleton-smoke", ok=True)
