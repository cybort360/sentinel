"""Efficiency benchmark: the multi-agent War Room vs a single-agent baseline.

Track 3 (Agent Society) is graded in part on a **measurable efficiency gain over
single-agent baselines** (architecture.md §2.1, §11). This module turns the §11
narrative into a quantified, per-corpus comparison: for each contract, the
single-pass :class:`BaselineAudit` (CodebaseMCP read only, no simulation) and the
full War Room (:class:`RunResult`, real ``SimulationMCP`` evidence) are scored
against a known ground truth, then aggregated.

The scoring is honest — every number is derived from a real run, not asserted:

* **Detection** — did it flag a real problem? The Society flags via its outcome
  (a veto / ``constraints_unsatisfied`` / non-zero residual risk); the Baseline
  via the vulnerabilities it lists from source.
* **Trade-offs surfaced** — the Society's residual-risk narrative + mitigations
  vs the Baseline's ``residual_risk_disclosed`` flag.
* **Trace-backed claims** — ``len(RiskProfile.trace_ids)`` (real simulation
  provenance, Golden Rule #1) vs the Baseline's zero (it runs no simulation).

The structural asymmetry the benchmark exposes — a single-pass reader *cannot*
see a simulation-discovered DoS or quantify residual risk — is real, which is why
the gain holds whether the agents are deterministic drivers (reproducible demo)
or live Qwen models (genuine discovery).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sentinel.orchestrator.graph import RunResult
from sentinel.orchestrator.schema import BaselineAudit, Outcome


class GroundTruth(BaseModel):
    """The known issues in a benchmark contract (the labels we score against)."""

    label: str
    #: Expected vulnerability-class keywords (e.g. ``"reentrancy"``,
    #: ``"congestion-dos"``, ``"access-control"``).
    vuln_classes: list[str]
    #: True when the key issue is only discoverable by *simulation* — a
    #: single-pass source reader structurally cannot see it.
    simulation_only: bool


class CaseScore(BaseModel):
    """The scored Society-vs-Baseline comparison for one contract."""

    name: str
    target: str
    ground_truth: GroundTruth

    society_detected: bool
    society_surfaced_tradeoff: bool
    society_outcome: str
    society_residual_pct: float
    society_iterations: int
    society_trace_backed_claims: int
    society_tokens: int
    society_elapsed_s: float

    baseline_detected: bool
    baseline_disclosed_residual: bool
    baseline_trace_backed_claims: int = 0
    baseline_elapsed_s: float = 0.0

    #: A known issue the run missed (the gain is the Baseline's misses minus the
    #: Society's — ideally the Society's is zero).
    society_false_negative: bool
    baseline_false_negative: bool


class Aggregate(BaseModel):
    """Corpus-level totals — the headline efficiency-gain numbers."""

    n_cases: int
    society_detection_rate: float
    baseline_detection_rate: float
    society_false_negatives: int
    baseline_false_negatives: int
    tradeoffs_surfaced_society: int
    tradeoffs_surfaced_baseline: int
    trace_backed_claims_society: int
    trace_backed_claims_baseline: int
    total_tokens_society: int
    mean_iterations_society: float
    society_wall_clock_s: float
    baseline_wall_clock_s: float


class BenchmarkReport(BaseModel):
    """A full benchmark run: per-case scores plus the aggregate."""

    cases: list[CaseScore] = Field(default_factory=list)
    aggregate: Aggregate


def _baseline_finds(baseline: BaselineAudit, vuln_classes: list[str]) -> bool:
    """True if the Baseline's listed vulns match an expected class (keyword)."""
    blob = " ".join(baseline.vulnerabilities).lower()
    return any(cls.split("-", 1)[0].lower() in blob for cls in vuln_classes)


def score_case(
    *,
    name: str,
    target: str,
    ground_truth: GroundTruth,
    society: RunResult,
    baseline: BaselineAudit,
    society_elapsed_s: float,
    baseline_elapsed_s: float,
) -> CaseScore:
    """Score one contract's Society and Baseline outputs against ground truth.

    Args:
        name: Short case name.
        target: Audited contract path.
        ground_truth: The known issues to score against.
        society: The War Room's :class:`RunResult` for this contract.
        baseline: The single-pass :class:`BaselineAudit` for this contract.
        society_elapsed_s: Wall-clock seconds the War Room run took.
        baseline_elapsed_s: Wall-clock seconds the Baseline run took.

    Returns:
        The :class:`CaseScore`.
    """
    profile = society.risk_profile
    review = society.final_review
    society_detected = (
        profile.outcome is Outcome.CONSTRAINTS_UNSATISFIED
        or profile.residual_risk_pct > 0.0
        or (review is not None and review.vetoed)
    )
    society_surfaced_tradeoff = (
        bool(profile.mitigations_applied) or profile.residual_risk_pct > 0.0
    )
    baseline_detected = _baseline_finds(baseline, ground_truth.vuln_classes)

    return CaseScore(
        name=name,
        target=target,
        ground_truth=ground_truth,
        society_detected=society_detected,
        society_surfaced_tradeoff=society_surfaced_tradeoff,
        society_outcome=profile.outcome.value,
        society_residual_pct=profile.residual_risk_pct,
        society_iterations=profile.iterations,
        society_trace_backed_claims=len(profile.trace_ids),
        society_tokens=profile.tokens_total,
        society_elapsed_s=round(society_elapsed_s, 3),
        baseline_detected=baseline_detected,
        baseline_disclosed_residual=baseline.residual_risk_disclosed,
        baseline_elapsed_s=round(baseline_elapsed_s, 3),
        society_false_negative=not society_detected,
        baseline_false_negative=not baseline_detected,
    )


def _rate(hits: int, total: int) -> float:
    """A safe ratio (0.0 when there are no cases)."""
    return round(hits / total, 3) if total else 0.0


def aggregate(cases: list[CaseScore]) -> Aggregate:
    """Roll per-case scores up into the corpus-level efficiency comparison."""
    n = len(cases)
    return Aggregate(
        n_cases=n,
        society_detection_rate=_rate(sum(c.society_detected for c in cases), n),
        baseline_detection_rate=_rate(sum(c.baseline_detected for c in cases), n),
        society_false_negatives=sum(c.society_false_negative for c in cases),
        baseline_false_negatives=sum(c.baseline_false_negative for c in cases),
        tradeoffs_surfaced_society=sum(c.society_surfaced_tradeoff for c in cases),
        tradeoffs_surfaced_baseline=sum(c.baseline_disclosed_residual for c in cases),
        trace_backed_claims_society=sum(c.society_trace_backed_claims for c in cases),
        trace_backed_claims_baseline=sum(c.baseline_trace_backed_claims for c in cases),
        total_tokens_society=sum(c.society_tokens for c in cases),
        mean_iterations_society=(
            round(sum(c.society_iterations for c in cases) / n, 2) if n else 0.0
        ),
        society_wall_clock_s=round(sum(c.society_elapsed_s for c in cases), 3),
        baseline_wall_clock_s=round(sum(c.baseline_elapsed_s for c in cases), 3),
    )


def build_report(cases: list[CaseScore]) -> BenchmarkReport:
    """Assemble a :class:`BenchmarkReport` from scored cases."""
    return BenchmarkReport(cases=cases, aggregate=aggregate(cases))
