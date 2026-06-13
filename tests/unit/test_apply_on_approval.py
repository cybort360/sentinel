"""Unit tests for the gated ``apply_approved_patch`` seam (architecture.md §8.6).

Covers every branch with a fake applier (no git/Anvil): approval applies, reject
/ no-patch are no-ops, and a CodebaseMCP failure degrades to ``None`` instead of
crashing (Rule 4). The real git + real-gate end-to-end lives in
``tests/integration/test_apply_on_approval.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from structlog.testing import capture_logs

from sentinel.mcp_servers.codebase_mcp.results import ApplyResult
from sentinel.orchestrator.checkpoint import (
    CheckpointDecision,
    CheckpointOutcome,
    CheckpointResponse,
    apply_approved_patch,
)


class _FakeApplier:
    """Records apply_patch calls; optionally raises to exercise the Rule-4 path."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[str] = []
        self._fail = fail

    def apply_patch(self, patch_id: str) -> ApplyResult:
        self.calls.append(patch_id)
        if self._fail:
            raise RuntimeError("git boom")
        return ApplyResult(
            patch_id=patch_id, path="contracts/X.sol", applied=True, commit="abc1234"
        )


def _outcome(decision: CheckpointDecision, approved: bool) -> CheckpointOutcome:
    return CheckpointOutcome(
        decision=decision,
        response=CheckpointResponse(decision=decision, responded_at=datetime.now(UTC)),
        approved=approved,
        escalated=decision is CheckpointDecision.ESCALATE,
    )


def test_approval_applies() -> None:
    applier = _FakeApplier()
    result = apply_approved_patch(
        _outcome(CheckpointDecision.APPROVE, True), codebase=applier, patch_id="p1"
    )
    assert result is not None and result.applied
    assert applier.calls == ["p1"]


def test_reject_does_not_apply() -> None:
    applier = _FakeApplier()
    result = apply_approved_patch(
        _outcome(CheckpointDecision.REJECT, False), codebase=applier, patch_id="p1"
    )
    assert result is None
    assert applier.calls == []


def test_no_patch_id_is_noop() -> None:
    applier = _FakeApplier()
    assert (
        apply_approved_patch(
            _outcome(CheckpointDecision.APPROVE, True), codebase=applier, patch_id=None
        )
        is None
    )
    assert applier.calls == []


def test_apply_failure_degrades() -> None:
    applier = _FakeApplier(fail=True)
    with capture_logs() as logs:
        result = apply_approved_patch(
            _outcome(CheckpointDecision.APPROVE, True), codebase=applier, patch_id="p1"
        )
    assert result is None  # degraded, not raised
    assert any("[DEGRADED]" in str(e.get("event", "")) for e in logs)
