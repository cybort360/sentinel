"""End-to-end gated apply: human approval is what reaches the canonical tree.

The Track 4 / §8-step-6 seam the rest of the suite did not cover: a staged
CodebaseMCP patch is applied to the canonical file **only** after the human
checkpoint approves, and a reject leaves the canonical file untouched. This runs
the *real* :class:`HumanCheckpoint` gate against a real git working copy (no
mocked git), so it proves the approve→apply wiring, not just the engine's
``apply_patch`` in isolation.

Run with: ``make check-integration`` (shells out to git).
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sentinel.mcp_servers.codebase_mcp.config import CodebaseConfig
from sentinel.mcp_servers.codebase_mcp.engine import CodebaseEngine
from sentinel.orchestrator.checkpoint import (
    CheckpointDecision,
    CheckpointOutcome,
    CheckpointResponse,
    DecisionPacket,
    HumanCheckpoint,
    apply_approved_patch,
)
from sentinel.orchestrator.schema import Outcome, Proposal, RiskProfile

pytestmark = pytest.mark.integration

_SANDBOX = Path(__file__).resolve().parents[2] / "sandbox" / "contracts"
_REL = "contracts/SubscriptionBilling.sol"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[CodebaseEngine]:
    """A CodebaseEngine over a temp git repo seeded with the billing contract."""
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    (tmp_path / "contracts").mkdir()
    shutil.copy(_SANDBOX / "SubscriptionBilling.sol", tmp_path / _REL)

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(tmp_path), *args], check=True, capture_output=True
        )

    git("init", "-b", "main")
    git("config", "user.email", "t@t.t")
    git("config", "user.name", "t")
    git("add", "-A")
    git("commit", "-m", "seed")
    yield CodebaseEngine(CodebaseConfig(repo_root=tmp_path))


class _Responder:
    """A non-blocking responder that returns a fixed decision (test double)."""

    def __init__(self, decision: CheckpointDecision) -> None:
        self._decision = decision

    async def ask(self, packet: DecisionPacket) -> CheckpointResponse:
        """Return the pre-set decision immediately."""
        return CheckpointResponse(
            decision=self._decision, responded_at=datetime.now(tz=UTC)
        )


def _packet(patch_id: str | None) -> DecisionPacket:
    proposal = (
        Proposal(
            proposal_id="proposal-apply",
            run_id="run-apply",
            iteration=1,
            patch_id=patch_id,
            summary="stage guarded billing contract",
            trace_ids=["sim-apply-1"],
        )
        if patch_id is not None
        else None
    )
    return DecisionPacket(
        run_id="run-apply",
        target=_REL,
        gated_reasons=["Applying a patch to the protocol (§7.1.2)"],
        proposal=proposal,
        assessment=None,
        review=None,
        risk_profile=RiskProfile(
            run_id="run-apply",
            outcome=Outcome.CONSENSUS,
            final_proposal=patch_id,
            residual_risk_pct=0.0,
            residual_risk_description="reentrancy guard closes the hole",
            iterations=1,
            tokens_total=0,
            trace_ids=["sim-apply-1"],
        ),
        memory_records_used=[],
        topic_tags=["reentrancy"],
    )


def _decide(decision: CheckpointDecision, *, patch_id: str | None) -> CheckpointOutcome:
    gate = HumanCheckpoint(_Responder(decision))
    return asyncio.run(gate.request(_packet(patch_id)))


def test_approval_applies_the_patch(engine: CodebaseEngine) -> None:
    canonical = engine._config.repo_root / _REL
    staged = engine.propose_patch(
        _REL, (_SANDBOX / "SubscriptionBillingGuarded.sol").read_text()
    )
    assert "nonReentrant" not in canonical.read_text()  # not applied yet

    outcome = _decide(CheckpointDecision.APPROVE, patch_id=staged.patch_id)
    result = apply_approved_patch(outcome, codebase=engine, patch_id=staged.patch_id)

    assert result is not None and result.applied
    assert "nonReentrant" in canonical.read_text()  # applied only after approval


def test_rejection_leaves_canonical_untouched(engine: CodebaseEngine) -> None:
    canonical = engine._config.repo_root / _REL
    before = canonical.read_text()
    staged = engine.propose_patch(
        _REL, (_SANDBOX / "SubscriptionBillingGuarded.sol").read_text()
    )

    outcome = _decide(CheckpointDecision.REJECT, patch_id=staged.patch_id)
    result = apply_approved_patch(outcome, codebase=engine, patch_id=staged.patch_id)

    assert result is None
    assert canonical.read_text() == before  # a reject never applies


def test_no_patch_id_cannot_be_approved() -> None:
    with pytest.raises(ValueError, match="staged patch_id"):
        _decide(CheckpointDecision.APPROVE, patch_id=None)
