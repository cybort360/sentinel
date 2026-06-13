"""Rule-1 enforcement test (CLAUDE.md §6 / Golden Rule #1).

Fails if any ``Veto``/``Proposal``/``RiskProfile`` — the objects that carry an
agent's factual claims about gas, revert rate, or exploit feasibility — can be
constructed without a real ``SimulationMCP`` ``trace_id``. ``AdversaryReview`` is
included for the same reason: a clearance is as much a simulation claim as a
veto. If someone deletes a ``trace_ids`` validator (or adds a new claim model
without one), one of these parametrized cases turns red.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import BaseModel, ValidationError

from sentinel.orchestrator.schema import (
    AdversaryReview,
    AgentRole,
    Outcome,
    Proposal,
    RiskProfile,
    Severity,
    Veto,
)


def _proposal(trace_ids: list[str]) -> Proposal:
    return Proposal(
        proposal_id="p1", run_id="r1", iteration=0, summary="x", trace_ids=trace_ids
    )


def _veto(trace_ids: list[str]) -> Veto:
    return Veto(
        veto_id="v1",
        run_id="r1",
        iteration=0,
        target_proposal_id="p1",
        issued_by=AgentRole.ADVERSARY,
        severity=Severity.HIGH,
        reason="fee spike reverts settlement",
        evidence="revert rate 1.0",
        trace_ids=trace_ids,
    )


def _risk_profile(trace_ids: list[str]) -> RiskProfile:
    return RiskProfile(
        run_id="r1",
        outcome=Outcome.CONSENSUS,
        residual_risk_pct=0.0,
        residual_risk_description="x",
        iterations=0,
        tokens_total=0,
        trace_ids=trace_ids,
    )


def _adversary_review(trace_ids: list[str]) -> AdversaryReview:
    return AdversaryReview(
        run_id="r1",
        iteration=0,
        target_proposal_id="p1",
        vetoed=True,
        severity=Severity.HIGH,
        reason="x",
        evidence="y",
        trace_ids=trace_ids,
    )


_BUILDERS: dict[str, Callable[[list[str]], BaseModel]] = {
    "Proposal": _proposal,
    "Veto": _veto,
    "RiskProfile": _risk_profile,
    "AdversaryReview": _adversary_review,
}
_CLAIM_MODELS = [Proposal, Veto, RiskProfile, AdversaryReview]


@pytest.mark.parametrize("name", list(_BUILDERS))
def test_a_real_trace_id_constructs_fine(name: str) -> None:
    obj = _BUILDERS[name](["simulationmcp-call-1"])
    assert obj.model_dump()["trace_ids"] == ["simulationmcp-call-1"]


@pytest.mark.parametrize("name", list(_BUILDERS))
def test_empty_trace_ids_is_rejected(name: str) -> None:
    # No SimulationMCP source -> unsourced claim -> must not be constructible.
    with pytest.raises(ValidationError):
        _BUILDERS[name]([])


@pytest.mark.parametrize("name", list(_BUILDERS))
def test_blank_trace_id_is_rejected(name: str) -> None:
    with pytest.raises(ValidationError):
        _BUILDERS[name](["   "])


@pytest.mark.parametrize("model", _CLAIM_MODELS)
def test_trace_ids_is_a_required_field(model: type[BaseModel]) -> None:
    # A claim object cannot default its way out of citing a source.
    assert "trace_ids" in model.model_fields
    assert model.model_fields["trace_ids"].is_required()


@pytest.mark.parametrize("model", _CLAIM_MODELS)
def test_constructing_without_trace_ids_raises(model: type[BaseModel]) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({})  # missing trace_ids (among others)
