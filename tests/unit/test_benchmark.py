"""Unit tests for the efficiency-benchmark scoring (architecture.md §2.1, §11).

Synthetic ``RunResult`` / ``BaselineAudit`` fixtures — no Anvil — exercise the
honest Society-vs-Baseline scoring and the corpus aggregate that is the Track 3
"measurable efficiency gain" deliverable.
"""

from __future__ import annotations

from sentinel.orchestrator.benchmark import (
    GroundTruth,
    aggregate,
    build_report,
    score_case,
)
from sentinel.orchestrator.graph import RunResult
from sentinel.orchestrator.schema import (
    AdversaryReview,
    BaselineAudit,
    Outcome,
    RiskProfile,
    Severity,
)


def _society(
    *,
    outcome: Outcome,
    residual: float,
    vetoed: bool,
    trace_ids: list[str],
    mitigations: list[str],
    iterations: int = 2,
    tokens: int = 0,
) -> RunResult:
    review = AdversaryReview(
        run_id="r",
        iteration=iterations,
        target_proposal_id="p",
        vetoed=vetoed,
        severity=Severity.HIGH if vetoed else None,
        reason="congestion DoS" if vetoed else "clear",
        evidence="get_revert_rate=1.0",
        trace_ids=trace_ids or ["t0"],
    )
    return RunResult(
        run_id="r",
        target="C.sol",
        final_review=review,
        risk_profile=RiskProfile(
            run_id="r",
            outcome=outcome,
            residual_risk_pct=residual,
            residual_risk_description="batching vs latency tension",
            mitigations_applied=mitigations,
            iterations=iterations,
            tokens_total=tokens,
            trace_ids=trace_ids or ["t0"],
        ),
    )


def _baseline(*, vulns: list[str], disclosed: bool) -> BaselineAudit:
    return BaselineAudit(
        vulnerabilities=vulns,
        recommended_fix="add a guard",
        residual_risk_disclosed=disclosed,
    )


def test_society_beats_baseline_on_a_simulation_only_issue() -> None:
    gt = GroundTruth(
        label="congestion DoS", vuln_classes=["congestion-dos"], simulation_only=True
    )
    score = score_case(
        name="billing",
        target="SubscriptionBilling.sol",
        ground_truth=gt,
        society=_society(
            outcome=Outcome.CONSTRAINTS_UNSATISFIED,
            residual=0.0004,
            vetoed=True,
            trace_ids=["a", "b", "c"],
            mitigations=["circuit breaker"],
        ),
        # single-pass source reader can't see a simulation-discovered DoS
        baseline=_baseline(vulns=["reentrancy in cancelSubscription"], disclosed=False),
        society_elapsed_s=0.2,
        baseline_elapsed_s=0.01,
    )
    assert score.society_detected and score.society_surfaced_tradeoff
    assert score.society_trace_backed_claims == 3
    assert not score.baseline_detected  # missed the congestion class
    assert score.baseline_false_negative and not score.society_false_negative
    assert not score.baseline_disclosed_residual


def test_baseline_can_catch_a_source_visible_vuln() -> None:
    gt = GroundTruth(
        label="access control", vuln_classes=["access-control"], simulation_only=False
    )
    score = score_case(
        name="vault",
        target="YieldVault.sol",
        ground_truth=gt,
        society=_society(
            outcome=Outcome.CONSTRAINTS_UNSATISFIED,
            residual=0.05,
            vetoed=True,
            trace_ids=["x"],
            mitigations=["onlyOwner gate"],
        ),
        baseline=_baseline(
            vulns=["missing access control on setOperator"], disclosed=False
        ),
        society_elapsed_s=0.2,
        baseline_elapsed_s=0.01,
    )
    assert score.baseline_detected  # "access" is visible from source
    assert not score.baseline_false_negative
    # ...but only the Society quantified residual risk / surfaced the trade-off
    assert score.society_surfaced_tradeoff and not score.baseline_disclosed_residual


def test_aggregate_reports_the_efficiency_gain() -> None:
    cases = [
        score_case(
            name="billing",
            target="SubscriptionBilling.sol",
            ground_truth=GroundTruth(
                label="congestion",
                vuln_classes=["congestion-dos"],
                simulation_only=True,
            ),
            society=_society(
                outcome=Outcome.CONSTRAINTS_UNSATISFIED,
                residual=0.0004,
                vetoed=True,
                trace_ids=["a", "b"],
                mitigations=["cb"],
            ),
            baseline=_baseline(vulns=["reentrancy"], disclosed=False),
            society_elapsed_s=0.2,
            baseline_elapsed_s=0.01,
        ),
        score_case(
            name="guarded",
            target="SubscriptionBillingGuarded.sol",
            ground_truth=GroundTruth(
                label="residual congestion",
                vuln_classes=["congestion-dos"],
                simulation_only=True,
            ),
            society=_society(
                outcome=Outcome.CONSTRAINTS_UNSATISFIED,
                residual=0.0004,
                vetoed=True,
                trace_ids=["c"],
                mitigations=["cb"],
            ),
            baseline=_baseline(vulns=[], disclosed=False),
            society_elapsed_s=0.2,
            baseline_elapsed_s=0.01,
        ),
    ]
    agg = aggregate(cases)
    assert agg.n_cases == 2
    assert agg.society_detection_rate == 1.0
    assert agg.baseline_detection_rate == 0.0
    assert agg.baseline_false_negatives == 2 and agg.society_false_negatives == 0
    assert agg.tradeoffs_surfaced_society == 2 and agg.tradeoffs_surfaced_baseline == 0
    assert agg.trace_backed_claims_society == 3  # 2 + 1
    assert agg.trace_backed_claims_baseline == 0

    report = build_report(cases)
    assert report.aggregate.n_cases == 2 and len(report.cases) == 2
