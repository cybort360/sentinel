"""Unit tests for memory decay scoring and the §6.3 forgetting rules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sentinel.memory.decay import (
    RECENCY_HALF_LIFE_DAYS,
    recency_decay,
    severity_rank,
    severity_weight,
)
from sentinel.memory.schema import MemoryRecord, MemoryStatus
from sentinel.memory.store import MemoryStore
from sentinel.orchestrator.schema import Severity

_NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _record(**kw: object) -> MemoryRecord:
    base: dict[str, object] = {
        "topic_tags": ["batching"],
        "severity": Severity.MEDIUM,
        "description": "d",
        "lesson_text": "l",
        "source_run_id": "r",
    }
    base.update(kw)
    return MemoryRecord(**base)  # type: ignore[arg-type]


def test_recency_decay_endpoints() -> None:
    assert recency_decay(_NOW, _NOW) == 1.0
    half = recency_decay(_NOW - timedelta(days=RECENCY_HALF_LIFE_DAYS), _NOW)
    assert abs(half - 0.5) < 1e-9
    # Monotonic: older -> smaller.
    older = recency_decay(_NOW - timedelta(days=360), _NOW)
    assert 0.0 < older < half


def test_severity_weight_and_rank_order() -> None:
    assert severity_weight(Severity.HIGH) > severity_weight(Severity.MEDIUM)
    assert severity_weight(Severity.MEDIUM) > severity_weight(Severity.LOW)
    assert severity_rank(Severity.HIGH) > severity_rank(Severity.LOW)


def test_decay_archives_records_past_relevance_horizon() -> None:
    store = MemoryStore()
    try:
        fresh = store.write_memory(_record(created_at=_NOW - timedelta(days=10)))
        # ~700 days -> recency_decay well below the 0.1 archive threshold.
        stale = store.write_memory(_record(created_at=_NOW - timedelta(days=700)))

        report = store.decay_memory(now=_NOW)

        assert report.archived_by_recency == 1
        got_stale = store.get(stale.id)
        got_fresh = store.get(fresh.id)
        assert got_stale is not None and got_stale.status is MemoryStatus.ARCHIVED
        assert got_fresh is not None and got_fresh.status is MemoryStatus.ACTIVE
    finally:
        store.close()


def test_decay_supersedes_older_record_with_subset_tags() -> None:
    store = MemoryStore()
    try:
        old = store.write_memory(
            _record(
                topic_tags=["batching", "latency"],
                severity=Severity.MEDIUM,
                created_at=_NOW - timedelta(days=20),
            )
        )
        # Newer, superset tags, higher severity -> supersedes `old`.
        new = store.write_memory(
            _record(
                topic_tags=["batching", "latency", "congestion"],
                severity=Severity.HIGH,
                created_at=_NOW - timedelta(days=5),
            )
        )

        report = store.decay_memory(now=_NOW)

        assert (old.id, new.id) in report.superseded_pairs
        got_old = store.get(old.id)
        got_new = store.get(new.id)
        assert got_old is not None
        assert got_old.status is MemoryStatus.ARCHIVED
        assert got_old.superseded_by == new.id
        assert got_new is not None and got_new.status is MemoryStatus.ACTIVE
    finally:
        store.close()


def test_decay_does_not_supersede_when_severity_is_higher() -> None:
    store = MemoryStore()
    try:
        # Older record is MORE severe than the newer one -> not superseded.
        old = store.write_memory(
            _record(
                topic_tags=["batching"],
                severity=Severity.HIGH,
                created_at=_NOW - timedelta(days=20),
            )
        )
        store.write_memory(
            _record(
                topic_tags=["batching", "latency"],
                severity=Severity.LOW,
                created_at=_NOW - timedelta(days=5),
            )
        )

        store.decay_memory(now=_NOW)

        got_old = store.get(old.id)
        assert got_old is not None and got_old.status is MemoryStatus.ACTIVE
    finally:
        store.close()
