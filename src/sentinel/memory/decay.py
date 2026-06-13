"""Scoring functions for memory retrieval and decay (architecture.md §6.1, §6.3).

These are pure, deterministic helpers — the load-bearing math behind both
``query_memory`` (the §6.1 composite score) and ``decay_memory`` (the §6.3
forgetting rules). They are unit-tested directly.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sentinel.orchestrator.schema import Severity

#: Multiplicative weight per severity in the §6.1 retrieval score (tunable).
SEVERITY_WEIGHTS: dict[Severity, float] = {
    Severity.HIGH: 1.0,
    Severity.MEDIUM: 0.6,
    Severity.LOW: 0.3,
}

#: Ordinal rank per severity, for the §6.3 "severity ≤ newer record" rule.
SEVERITY_RANK: dict[Severity, int] = {
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
}

#: Half-life for recency decay (architecture.md §6.1).
RECENCY_HALF_LIFE_DAYS = 180.0

#: Records whose recency decay drops below this are archived (architecture.md §6.3).
RECENCY_ARCHIVE_THRESHOLD = 0.1


def severity_weight(severity: Severity) -> float:
    """Return the multiplicative retrieval weight for a severity (§6.1)."""
    return SEVERITY_WEIGHTS[severity]


def severity_rank(severity: Severity) -> int:
    """Return the ordinal rank for a severity (higher = more severe)."""
    return SEVERITY_RANK[severity]


def recency_decay(
    created_at: datetime,
    now: datetime | None = None,
    half_life_days: float = RECENCY_HALF_LIFE_DAYS,
) -> float:
    """Exponential recency decay in (0, 1] (architecture.md §6.1).

    Args:
        created_at: When the record was created (tz-aware).
        now: Reference time; defaults to the current UTC time.
        half_life_days: Days after which the weight halves.

    Returns:
        ``0.5 ** (age_days / half_life_days)`` — 1.0 at age 0, 0.5 at one
        half-life, never negative.
    """
    reference = now if now is not None else datetime.now(tz=UTC)
    age_days = max(0.0, (reference - created_at).total_seconds() / 86400.0)
    return float(0.5 ** (age_days / half_life_days))
