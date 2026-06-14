"""Unit tests for the War Room orchestration graph (architecture.md §8, §9).

Every agent is a scripted fake — no LLM, no MCP servers — so these tests pin the
*control flow*: the early-exit path, consensus and constraints-unsatisfied
termination, the ``max_iterations`` cap, run-level Golden-Rule-1 trace
accumulation, and Rule-4 ingest degradation.
"""

from __future__ import annotations

from types import SimpleNamespace

from sentinel.orchestrator.graph import RunResult, WarRoomGraph
from sentinel.orchestrator.schema import (
    AdversaryReview,
    BaselineAudit,
    Constraint,
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
    # Narrative comes from the draft; accounting is the graph's real data.
    assert profile.residual_risk_pct == 0.04
    assert profile.trace_ids == ["sim-scan-1", "sim-v1"]  # deduped, no draft-bogus
    assert "draft-bogus" not in profile.trace_ids
    assert arb.propose_calls == 1 and arb.synth_calls == 1


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
