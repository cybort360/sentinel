"""Unit tests for the War Room orchestration graph (architecture.md §8, §9).

Every agent is a scripted fake — no LLM, no MCP servers — so these tests pin the
*control flow*: the early-exit path, consensus and constraints-unsatisfied
termination, the ``max_iterations`` cap, run-level Golden-Rule-1 trace
accumulation, and Rule-4 ingest degradation.
"""

from __future__ import annotations

from types import SimpleNamespace

from structlog.testing import capture_logs

from sentinel.mcp_servers.codebase_mcp.results import ProposeResult
from sentinel.orchestrator.graph import RunResult, WarRoomGraph, _initial_adversary_task
from sentinel.orchestrator.schema import (
    AdversaryReview,
    BaselineAudit,
    Constraint,
    DynamicVerificationStatus,
    LessonsContext,
    Outcome,
    Proposal,
    Resolution,
    RiskProfile,
    Severity,
    YieldAssessment,
    YieldVerdict,
)
from sentinel.orchestrator.token_budget import BudgetConfig

# --------------------------------------------------------------------------- #
# Scripted fakes
# --------------------------------------------------------------------------- #


class FakeYield:
    model = "fake-yield"

    def __init__(self, results: list[YieldAssessment]) -> None:
        self._q = list(results)
        self.tasks: list[str] = []

    def assess(self, task: str) -> YieldAssessment:
        self.tasks.append(task)
        return self._q.pop(0)


class FakeAdversary:
    model = "fake-adversary"

    def __init__(self, results: list[AdversaryReview]) -> None:
        self._q = list(results)
        self.tasks: list[str] = []

    def review(self, task: str) -> AdversaryReview:
        self.tasks.append(task)
        return self._q.pop(0)


class FakeArbitrator:
    model = "fake-arbitrator"

    def __init__(
        self, proposals: list[Proposal], draft: RiskProfile | None = None
    ) -> None:
        self._proposals = list(proposals)
        self._draft = draft
        self.propose_calls = 0
        self.synth_calls = 0

    def propose(self, task: str) -> Proposal:
        self.propose_calls += 1
        return self._proposals.pop(0)

    def synthesize(self, task: str) -> RiskProfile:
        self.synth_calls += 1
        assert self._draft is not None
        return self._draft


class FakeLessons:
    model = "fake-lessons"

    def __init__(self, contexts: list[LessonsContext] | None = None) -> None:
        self._q = list(contexts or [])
        self.calls = 0

    def recall(self, task: str) -> LessonsContext:
        self.calls += 1
        return self._q.pop(0) if self._q else LessonsContext()


class FakeBaseline:
    model = "fake-baseline"

    def __init__(self, audit: BaselineAudit) -> None:
        self._audit = audit
        self.calls = 0

    def audit(self, task: str) -> BaselineAudit:
        self.calls += 1
        return self._audit


class FakeReaderWithArtifact:
    def __init__(self, artifact: str) -> None:
        self._artifact = artifact

    def read_contract(self, path: str) -> object:
        return SimpleNamespace(summary=SimpleNamespace(unauditable_functions=[]))

    def staged_artifact(self, patch_id: str) -> str | None:
        return self._artifact


class FakeReaderWithLatestPatch:
    def __init__(self, result: ProposeResult) -> None:
        self._results = [None, result]
        self.latest_patch_calls = 0

    def read_contract(self, path: str) -> object:
        return SimpleNamespace(summary=SimpleNamespace(unauditable_functions=[]))

    def latest_patch(self) -> ProposeResult | None:
        self.latest_patch_calls += 1
        if self._results:
            return self._results.pop(0)
        return None


def _reader(unauditable: list[str] | None = None, fail: bool = False) -> object:
    class _R:
        def read_contract(self, path: str) -> object:
            if fail:
                raise RuntimeError("anvil parse exploded")
            return SimpleNamespace(
                summary=SimpleNamespace(unauditable_functions=list(unauditable or []))
            )

    return _R()


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def _yield(verdict: YieldVerdict) -> YieldAssessment:
    return YieldAssessment(
        run_id="r",
        iteration=0,
        verdict=verdict,
        rationale="because processBilling",
        referenced_functions=["processBilling"],
    )


def _review(vetoed: bool, traces: list[str]) -> AdversaryReview:
    return AdversaryReview(
        run_id="r",
        iteration=0,
        target_proposal_id="p",
        vetoed=vetoed,
        severity=Severity.HIGH if vetoed else None,
        reason="fee spike reverts settlement" if vetoed else "clean under sweep",
        evidence="revert rate 1.0" if vetoed else "revert rate 0.0",
        trace_ids=traces,
    )


def _proposal(patch_id: str, traces: list[str]) -> Proposal:
    return Proposal(
        proposal_id=f"prop-{patch_id}",
        run_id="r",
        iteration=1,
        patch_id=patch_id,
        summary="add nonReentrant guard",
        trace_ids=traces,
    )


def _draft(outcome: Outcome = Outcome.CONSTRAINTS_UNSATISFIED) -> RiskProfile:
    # The synthesize draft deliberately carries values the graph must OVERRIDE:
    # a contradicting outcome and bogus trace_ids, to prove the graph is the
    # authoritative source for accounting fields (Rule 1 at run level).
    return RiskProfile(
        run_id="DRAFT",
        outcome=outcome,
        final_proposal="DRAFT-PATCH",
        residual_risk_pct=0.04,
        residual_risk_description="reentrancy guard holds; 12.9% gas cost",
        mitigations_applied=["circuit-breaker on revert rate > 5%"],
        iterations=99,
        tokens_total=99999,
        trace_ids=["draft-bogus"],
    )


def _graph(
    *,
    yields: list[YieldAssessment],
    reviews: list[AdversaryReview],
    proposals: list[Proposal] | None = None,
    draft: RiskProfile | None = None,
    lessons: list[LessonsContext] | None = None,
    max_iterations: int = 4,
    baseline: FakeBaseline | None = None,
    reader: object | None = None,
) -> tuple[WarRoomGraph, FakeArbitrator]:
    arb = FakeArbitrator(proposals or [], draft)
    graph = WarRoomGraph(
        yield_agent=FakeYield(yields),
        adversary=FakeAdversary(reviews),
        arbitrator=arb,
        lessons=FakeLessons(lessons),
        codebase=reader or _reader(),
        budget=BudgetConfig(max_iterations=max_iterations),
        baseline=baseline,
    )
    return graph, arb


# --------------------------------------------------------------------------- #
# Early exit (§9): clean scan + Yield accepts -> no loop, empty profile
# --------------------------------------------------------------------------- #


def test_early_exit_on_clean_scan_skips_loop() -> None:
    graph, arb = _graph(
        yields=[_yield(YieldVerdict.ACCEPT)],
        reviews=[_review(vetoed=False, traces=["sim-scan-1"])],
    )
    result = graph.run("x.sol", run_id="run-1")
    assert isinstance(result, RunResult)
    assert result.risk_profile.outcome is Outcome.CONSENSUS
    assert result.risk_profile.iterations == 0
    assert result.risk_profile.residual_risk_pct == 0.0
    assert result.final_proposal is None
    assert result.risk_profile.trace_ids == ["sim-scan-1"]  # Rule 1 even on exit
    # The loop never ran: the Arbitrator was never consulted.
    assert arb.propose_calls == 0 and arb.synth_calls == 0


# --------------------------------------------------------------------------- #
# Consensus path: anomaly found, one patch round reaches agreement
# --------------------------------------------------------------------------- #


def test_consensus_after_one_round() -> None:
    graph, arb = _graph(
        yields=[_yield(YieldVerdict.REVISE), _yield(YieldVerdict.ACCEPT)],
        reviews=[
            _review(vetoed=True, traces=["sim-scan-1"]),  # initial anomaly
            _review(vetoed=False, traces=["sim-v1"]),  # patch clears it
        ],
        proposals=[_proposal("patch-1", traces=["sim-scan-1"])],
        draft=_draft(outcome=Outcome.CONSTRAINTS_UNSATISFIED),
    )
    result = graph.run("x.sol", run_id="run-2")
    profile = result.risk_profile
    assert profile.outcome is Outcome.CONSENSUS  # graph overrides the draft
    assert profile.iterations == 1
    assert result.final_proposal is not None
    assert profile.final_proposal == "patch-1"
    # Risk text comes from the final agent positions; accounting is the graph's
    # real data.
    assert profile.residual_risk_pct == 0.04
    assert "Final Adversary review cleared the proposal" in (
        profile.residual_risk_description
    )
    assert "clean under sweep" in profile.residual_risk_description
    assert profile.trace_ids == ["sim-scan-1", "sim-v1"]  # deduped, no draft-bogus
    assert "draft-bogus" not in profile.trace_ids
    assert arb.propose_calls == 1 and arb.synth_calls == 1


def test_arbitrator_proposal_cannot_introduce_unknown_trace_ids() -> None:
    graph, _ = _graph(
        yields=[_yield(YieldVerdict.REVISE), _yield(YieldVerdict.ACCEPT)],
        reviews=[
            _review(vetoed=True, traces=["sim-scan-1"]),
            _review(vetoed=False, traces=["sim-v1"]),
        ],
        proposals=[_proposal("patch-1", traces=["fake-trace"])],
        draft=_draft(),
    )
    result = graph.run("x.sol", run_id="run-fake-proposal-trace")

    assert result.final_proposal is not None
    assert result.final_proposal.trace_ids == ["sim-scan-1"]
    assert result.risk_profile.trace_ids == ["sim-scan-1", "sim-v1"]
    assert "fake-trace" not in result.risk_profile.trace_ids


def test_missing_patch_id_terminates_as_tool_failure() -> None:
    proposal = Proposal(
        proposal_id="prop-missing",
        run_id="r",
        iteration=1,
        patch_id=None,
        summary="patch staging failed",
        trace_ids=["sim-scan-1"],
    )
    graph, _ = _graph(
        yields=[_yield(YieldVerdict.REVISE)],
        reviews=[_review(vetoed=True, traces=["sim-scan-1"])],
        proposals=[proposal],
        draft=_draft(),
        max_iterations=1,
    )

    result = graph.run("x.sol", run_id="run-missing-patch")

    assert result.final_proposal is None
    assert result.final_review is not None
    assert result.final_review.vetoed
    assert result.risk_profile.outcome is Outcome.TOOL_FAILURE
    assert result.risk_profile.iterations == 1
    assert result.risk_profile.final_proposal is None
    assert "proposal has no patch_id" in result.risk_profile.residual_risk_description


def test_missing_model_patch_id_uses_new_codebase_patch_result() -> None:
    staged = ProposeResult(
        patch_id="real-staged-patch",
        path="sandbox/contracts/audit_workdir/Vault.sol",
        branch="sentinel/patch-real",
        base_commit="abc123",
        staged_source_path="sandbox/contracts/staged_patches/real-staged-patch.sol",
        staged_artifact=(
            "sandbox/contracts/staged_patches/real-staged-patch.sol:Vault"
        ),
        contract_name="Vault",
        artifact_path="sandbox/out/real-staged-patch.sol/Vault.json",
        original_target_path="sandbox/contracts/audit_workdir/Vault.sol",
    )
    proposal = Proposal(
        proposal_id="prop_vsv_round1_v1",
        run_id="r",
        iteration=1,
        patch_id=None,
        summary="stage uploaded vault patch",
        trace_ids=["sim-scan-1"],
    )
    reader = FakeReaderWithLatestPatch(staged)
    graph, _ = _graph(
        yields=[_yield(YieldVerdict.REVISE), _yield(YieldVerdict.ACCEPT)],
        reviews=[
            _review(vetoed=True, traces=["sim-scan-1"]),
            _review(vetoed=False, traces=["sim-v1"]),
        ],
        proposals=[proposal],
        draft=_draft(outcome=Outcome.CONSTRAINTS_UNSATISFIED),
        max_iterations=1,
        reader=reader,
    )

    result = graph.run(
        "sandbox/contracts/audit_workdir/Vault.sol",
        run_id="run-model-omitted-patch-id",
    )

    assert result.risk_profile.outcome is Outcome.CONSENSUS
    assert result.final_proposal is not None
    assert result.final_proposal.proposal_id == "prop_vsv_round1_v1"
    assert result.final_proposal.patch_id == "real-staged-patch"
    assert result.final_proposal.staged_artifact == staged.staged_artifact
    assert result.risk_profile.final_proposal == "real-staged-patch"
    assert reader.latest_patch_calls >= 2


def test_propose_exception_terminates_as_tool_failure() -> None:
    class FailingArbitrator(FakeArbitrator):
        def propose(self, task: str) -> Proposal:
            self.propose_calls += 1
            raise RuntimeError("git add failed: ignored path")

    arb = FailingArbitrator([], _draft())
    graph = WarRoomGraph(
        yield_agent=FakeYield([_yield(YieldVerdict.REVISE)]),
        adversary=FakeAdversary([_review(vetoed=True, traces=["sim-scan-1"])]),
        arbitrator=arb,
        lessons=FakeLessons([LessonsContext()]),
        codebase=_reader(),
        budget=BudgetConfig(max_iterations=4),
    )

    result = graph.run("x.sol", run_id="run-propose-failed")

    assert result.risk_profile.outcome is Outcome.TOOL_FAILURE
    assert result.risk_profile.iterations == 1
    assert result.risk_profile.final_proposal is None
    assert result.negotiation == []
    assert arb.propose_calls == 1


def test_patch_verification_failure_terminates_without_veto_loop() -> None:
    failed_review = AdversaryReview(
        run_id="r",
        iteration=1,
        target_proposal_id="prop-patch-1",
        vetoed=True,
        severity=Severity.HIGH,
        reason="Staged patch cannot be deployed because build artifact is missing",
        evidence="deploy_to_fork returned a degraded missing artifact trace",
        trace_ids=["sim-deploy-failed"],
    )
    graph, arb = _graph(
        yields=[_yield(YieldVerdict.REVISE), _yield(YieldVerdict.ACCEPT)],
        reviews=[
            _review(vetoed=True, traces=["sim-scan-1"]),
            failed_review,
        ],
        proposals=[_proposal("patch-1", traces=["sim-scan-1"])],
        draft=_draft(),
        max_iterations=4,
    )

    result = graph.run("x.sol", run_id="run-patch-verify-failed")

    assert result.risk_profile.outcome is Outcome.TOOL_FAILURE
    assert result.risk_profile.iterations == 1
    assert result.negotiation == []
    assert arb.propose_calls == 1


def test_dynamic_verification_unavailable_preserves_static_patch() -> None:
    unavailable = AdversaryReview(
        run_id="r",
        iteration=1,
        target_proposal_id="prop-patch-1",
        vetoed=True,
        severity=Severity.HIGH,
        reason=(
            "dynamic_verification_unavailable: unsupported constructor argument "
            "owners:address[]"
        ),
        evidence="infer_constructor_args reported arrays are unsupported",
        trace_ids=["sim-constructor-unavailable"],
    )
    graph, arb = _graph(
        yields=[_yield(YieldVerdict.REVISE), _yield(YieldVerdict.ACCEPT)],
        reviews=[
            _review(vetoed=True, traces=["sim-scan-1"]),
            unavailable,
        ],
        proposals=[_proposal("patch-1", traces=["sim-scan-1"])],
        draft=_draft(),
        max_iterations=4,
    )

    with capture_logs() as logs:
        result = graph.run(
            "sandbox/contracts/audit_workdir/Unsupported.sol",
            run_id="run-dynamic-unavailable",
        )

    assert result.risk_profile.outcome is Outcome.DYNAMIC_VERIFICATION_UNAVAILABLE
    assert result.risk_profile.final_proposal == "patch-1"
    assert result.final_proposal is not None
    assert result.risk_profile.dynamic_verification_status is (
        DynamicVerificationStatus.UNAVAILABLE
    )
    assert "Dynamic verification was unavailable" in (
        result.risk_profile.residual_risk_description
    )
    assert result.negotiation == []
    assert arb.propose_calls == 1
    risk_logs = [
        entry for entry in logs if entry.get("demo_tag") == "ANNOTATED RESIDUAL RISK"
    ]
    assert risk_logs
    summary = str(risk_logs[-1].get("event", ""))
    assert summary.startswith("Unverified - ")
    assert "0%" not in summary


def test_uploaded_patch_review_uses_compiled_staged_artifact() -> None:
    artifact = "sandbox/contracts/staged_patches/patch-1/Vault.sol:Vault"
    reader = FakeReaderWithArtifact(artifact)
    graph, _ = _graph(
        yields=[_yield(YieldVerdict.REVISE), _yield(YieldVerdict.ACCEPT)],
        reviews=[
            _review(vetoed=True, traces=["sim-scan-1"]),
            _review(vetoed=False, traces=["sim-clear-1"]),
        ],
        proposals=[_proposal("patch-1", traces=["sim-scan-1"])],
        draft=_draft(Outcome.CONSENSUS),
        reader=reader,
    )

    result = graph.run("sandbox/contracts/audit_workdir/Vault.sol", run_id="run-art")

    assert result.final_proposal is not None
    assert result.final_proposal.staged_artifact == artifact
    assert artifact in graph._adversary.tasks[-1]


# --------------------------------------------------------------------------- #
# Constraints-unsatisfied path + max_iterations cap
# --------------------------------------------------------------------------- #


def test_constraints_unsatisfied_respects_max_iterations() -> None:
    graph, arb = _graph(
        yields=[_yield(YieldVerdict.REVISE)] * 3,  # initial + 2 rounds, never accept
        reviews=[_review(vetoed=True, traces=[f"sim-{i}"]) for i in range(3)],
        proposals=[_proposal(f"patch-{n}", traces=[f"sim-p{n}"]) for n in (1, 2)],
        draft=_draft(outcome=Outcome.CONSENSUS),  # draft lies; graph must override
        lessons=[
            LessonsContext(memory_records_used=["mem-a"]),  # initial
            LessonsContext(memory_records_used=["mem-b"]),  # round-1 veto
            LessonsContext(memory_records_used=["mem-a"]),  # round-2 veto (dup)
        ],
        max_iterations=2,
    )
    result = graph.run("x.sol", run_id="run-3")
    profile = result.risk_profile
    assert profile.outcome is Outcome.CONSTRAINTS_UNSATISFIED
    assert profile.iterations == 2
    assert arb.propose_calls == 2  # hard cap respected — not 3+
    assert profile.final_proposal == "patch-2"  # last staged candidate
    assert profile.memory_records_used == ["mem-a", "mem-b"]  # accumulated + deduped
    assert profile.trace_ids  # non-empty (Rule 1)
    assert "Final Adversary veto remains unresolved" in (
        profile.residual_risk_description
    )


def test_higher_cap_allows_more_rounds() -> None:
    # Same never-consensus setup but cap=4 -> exactly 4 proposal rounds.
    graph, arb = _graph(
        yields=[_yield(YieldVerdict.REVISE)] * 5,
        reviews=[_review(vetoed=True, traces=[f"s{i}"]) for i in range(5)],
        proposals=[_proposal(f"patch-{n}", traces=[f"sp{n}"]) for n in range(1, 5)],
        draft=_draft(),
        max_iterations=4,
    )
    result = graph.run("x.sol", run_id="run-4")
    assert result.risk_profile.iterations == 4
    assert arb.propose_calls == 4


# --------------------------------------------------------------------------- #
# Ingest / Track-4 + baseline wiring
# --------------------------------------------------------------------------- #


def test_unauditable_functions_surfaced_from_ingest() -> None:
    graph, _ = _graph(
        yields=[_yield(YieldVerdict.ACCEPT)],
        reviews=[_review(vetoed=False, traces=["sim-1"])],
        reader=_reader(unauditable=["brokenFn"]),
    )
    result = graph.run("x.sol")
    assert result.unauditable_functions == ["brokenFn"]


def test_ingest_failure_degrades_and_run_continues() -> None:
    # Rule 4: a read_contract crash must not abort the audit.
    graph, _ = _graph(
        yields=[_yield(YieldVerdict.ACCEPT)],
        reviews=[_review(vetoed=False, traces=["sim-1"])],
        reader=_reader(fail=True),
    )
    result = graph.run("x.sol")
    assert result.unauditable_functions == []
    assert result.risk_profile.outcome is Outcome.CONSENSUS  # still produced


def test_baseline_runs_when_configured() -> None:
    audit = BaselineAudit(
        vulnerabilities=["reentrancy"],
        recommended_fix="add guard",
        referenced_functions=["cancelSubscription"],
    )
    baseline = FakeBaseline(audit)
    graph, _ = _graph(
        yields=[_yield(YieldVerdict.ACCEPT)],
        reviews=[_review(vetoed=False, traces=["sim-1"])],
        baseline=baseline,
    )
    result = graph.run("x.sol")
    assert baseline.calls == 1
    assert result.baseline == audit


def test_run_id_is_generated_when_omitted() -> None:
    graph, _ = _graph(
        yields=[_yield(YieldVerdict.ACCEPT)],
        reviews=[_review(vetoed=False, traces=["sim-1"])],
    )
    result = graph.run("x.sol")
    assert result.run_id and result.risk_profile.run_id == result.run_id


def test_constraints_from_memory_feed_next_proposal_task() -> None:
    # A veto-triggered recall injects a constraint that must reach the next
    # propose() task string (§8 step f, §9 windowing).
    seen_tasks: list[str] = []

    class RecordingArbitrator(FakeArbitrator):
        def propose(self, task: str) -> Proposal:
            seen_tasks.append(task)
            return super().propose(task)

    arb = RecordingArbitrator(
        [_proposal("patch-1", ["sp1"]), _proposal("patch-2", ["sp2"])],
        _draft(),
    )
    graph = WarRoomGraph(
        yield_agent=FakeYield([_yield(YieldVerdict.REVISE)] * 3),
        adversary=FakeAdversary(
            [_review(vetoed=True, traces=[f"s{i}"]) for i in range(3)]
        ),
        arbitrator=arb,
        lessons=FakeLessons(
            [
                LessonsContext(),  # initial
                LessonsContext(
                    constraints=[
                        Constraint(
                            constraint_text="must hold finality under N blocks",
                            topic_tags=["batching"],
                            source_memory_id="mem-b",
                        )
                    ],
                    memory_records_used=["mem-b"],
                ),
            ]
        ),
        codebase=_reader(),
        budget=BudgetConfig(max_iterations=2),
    )
    graph.run("x.sol", run_id="run-5")
    # Round 2's proposal task must mention the constraint pulled after round 1.
    assert any("finality under N blocks" in t for t in seen_tasks)


# --------------------------------------------------------------------------- #
# Negotiation / conflict resolution (architecture.md §4.3, Track 3)
# --------------------------------------------------------------------------- #


def test_consensus_round_is_recorded_as_reconciled() -> None:
    graph, _ = _graph(
        yields=[_yield(YieldVerdict.REVISE), _yield(YieldVerdict.ACCEPT)],
        reviews=[
            _review(vetoed=True, traces=["sim-scan-1"]),
            _review(vetoed=False, traces=["sim-v1"]),
        ],
        proposals=[_proposal("patch-1", traces=["sim-scan-1"])],
        draft=_draft(),
    )
    result = graph.run("x.sol", run_id="run-recon")
    assert [a.resolution for a in result.negotiation] == [Resolution.RECONCILED]
    # The ruling is sourced (Rule 1) — it cites the round's clearance trace.
    assert result.negotiation[0].trace_ids == ["sim-v1"]


def test_deadlock_records_veto_upheld_then_unresolved() -> None:
    graph, _ = _graph(
        yields=[_yield(YieldVerdict.REVISE)] * 3,
        reviews=[_review(vetoed=True, traces=[f"sim-{i}"]) for i in range(3)],
        proposals=[_proposal(f"patch-{i}", traces=[f"sim-p{i}"]) for i in range(2)],
        draft=_draft(),
        max_iterations=2,
    )
    result = graph.run("x.sol", run_id="run-deadlock")
    resolutions = [a.resolution for a in result.negotiation]
    # Two rounds: the first upholds the veto, the final one is a deadlock.
    assert resolutions == [Resolution.VETO_UPHELD, Resolution.UNRESOLVED]
    assert all(a.trace_ids for a in result.negotiation)  # every ruling is sourced


def test_early_exit_has_no_negotiation() -> None:
    graph, _ = _graph(
        yields=[_yield(YieldVerdict.ACCEPT)],
        reviews=[_review(vetoed=False, traces=["sim-scan-1"])],
    )
    result = graph.run("x.sol", run_id="run-exit")
    assert result.negotiation == []  # no conflict arose


def test_uploaded_contract_initial_task_uses_generic_mode() -> None:
    task = _initial_adversary_task(
        "sandbox/contracts/audit_workdir/VulnerableSubscriptionVault.sol"
    )

    assert "generic probes only" in task
    assert "Do not run demo scenarios" in task
    assert "fee_spike" in task
    assert "static finding only; no exploit trace available" in task
