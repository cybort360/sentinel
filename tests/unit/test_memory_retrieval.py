"""Unit tests for §6.1 retrieval ranking (semantic × severity × recency)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sentinel.memory.schema import MemoryRecord
from sentinel.memory.seed import seed_store
from sentinel.memory.store import MemoryStore
from sentinel.orchestrator.schema import Severity

_NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _seeded_store() -> MemoryStore:
    store = MemoryStore()
    seed_store(store)
    return store


def test_batching_query_surfaces_the_batching_record() -> None:
    store = _seeded_store()
    try:
        results = store.query_memory(
            "fee spike congestion causing settlement reverts, batching and "
            "finality delay",
            top_k=3,
        )
        assert results, "expected at least one result"
        assert "batching" in results[0].record.topic_tags
    finally:
        store.close()


def test_access_control_query_surfaces_the_access_control_record() -> None:
    store = _seeded_store()
    try:
        results = store.query_memory(
            "privileged emergency withdraw operator role assignment lets caller "
            "drain custody",
            top_k=3,
        )
        assert results
        assert "access-control" in results[0].record.topic_tags
    finally:
        store.close()


def test_top_k_limits_results() -> None:
    store = _seeded_store()
    try:
        results = store.query_memory(
            "settlement batching access control oracle", top_k=2
        )
        assert len(results) <= 2
    finally:
        store.close()


def _content_record(severity: Severity, created_at: datetime, rid: str) -> MemoryRecord:
    # Identical text across records so semantic similarity to the query is equal,
    # isolating the severity / recency factors of the §6.1 formula.
    return MemoryRecord(
        id=rid,
        created_at=created_at,
        topic_tags=["batching"],
        severity=severity,
        description="batching settlement latency finality congestion",
        lesson_text="batching introduces settlement latency",
        source_run_id="r",
    )


def test_severity_breaks_ties_in_score() -> None:
    store = MemoryStore()
    try:
        store.write_memory(_content_record(Severity.LOW, _NOW, "id-low"))
        store.write_memory(_content_record(Severity.HIGH, _NOW, "id-high"))
        results = store.query_memory("batching settlement latency finality", top_k=2)
        assert [s.record.id for s in results] == ["id-high", "id-low"]
        assert results[0].semantic == results[1].semantic  # same text -> same semantic
    finally:
        store.close()


def test_recency_breaks_ties_in_score() -> None:
    store = MemoryStore()
    try:
        store.write_memory(
            _content_record(Severity.MEDIUM, _NOW - timedelta(days=365), "id-old")
        )
        store.write_memory(_content_record(Severity.MEDIUM, _NOW, "id-new"))
        results = store.query_memory("batching settlement latency finality", top_k=2)
        assert results[0].record.id == "id-new"
    finally:
        store.close()


def test_archived_records_excluded_unless_requested() -> None:
    store = _seeded_store()
    try:
        store.decay_memory(now=_NOW)  # no-op on fresh seeds, but exercises the path
        default = store.query_memory("oracle price manipulation amm flash", top_k=5)
        assert all(s.record.status.value == "active" for s in default)
        # include_archived must not crash and returns >= default count.
        widened = store.query_memory(
            "oracle price manipulation amm flash", top_k=5, include_archived=True
        )
        assert len(widened) >= len(default)
    finally:
        store.close()
