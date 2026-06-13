"""The MemoryMCP store: SQLite + sqlite-vec (architecture.md §5.3, §6).

Structured fields live in a regular SQLite table; embeddings live in a
``sqlite-vec`` ``vec0`` virtual table keyed by the record's rowid. Retrieval
fetches cosine-nearest candidates from sqlite-vec, then re-ranks them by the
§6.1 composite score (``semantic * severity_weight * recency``) — the multiply
can't be expressed by vector distance alone. ``decay_memory`` implements the
§6.3 forgetting rules (recency archival + supersession).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import sqlite_vec

from sentinel.memory.decay import (
    RECENCY_ARCHIVE_THRESHOLD,
    recency_decay,
    severity_rank,
    severity_weight,
)
from sentinel.memory.embeddings import Embedder, HashingEmbedder
from sentinel.memory.schema import (
    DecayReport,
    MemoryRecord,
    MemoryStatus,
    ScoredMemory,
)
from sentinel.observability.trace_logger import get_logger
from sentinel.orchestrator.schema import Severity

_log = get_logger("memory_mcp")


class MemoryStore:
    """A persistent, decaying store of post-mortem memory records."""

    def __init__(
        self, db_path: str | Path = ":memory:", embedder: Embedder | None = None
    ) -> None:
        """Open (and initialise) the store at ``db_path``.

        Args:
            db_path: SQLite path, or ``":memory:"`` for an ephemeral store.
            embedder: Embedding backend; defaults to :class:`HashingEmbedder`.
        """
        self._embedder = embedder or HashingEmbedder()
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)
        self._init_schema()

    def _init_schema(self) -> None:
        """Create the records table and the sqlite-vec embedding table."""
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_records (
                id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                topic_tags TEXT NOT NULL,
                severity TEXT NOT NULL,
                description TEXT NOT NULL,
                lesson_text TEXT NOT NULL,
                source_run_id TEXT NOT NULL,
                superseded_by TEXT,
                status TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec "
            f"USING vec0(embedding float[{self._embedder.dim}] distance_metric=cosine)"
        )
        self._conn.commit()

    # -- tools ----------------------------------------------------------------

    def write_memory(self, record: MemoryRecord) -> MemoryRecord:
        """Insert (or replace) a record and its embedding (architecture.md §5.3).

        Args:
            record: The post-mortem record to store. Re-using an existing ``id``
                replaces the prior version (keeps seeding idempotent).

        Returns:
            The stored record.
        """
        embedding = self._embedder.embed(f"{record.description} {record.lesson_text}")
        cur = self._conn.cursor()
        existing = cur.execute(
            "SELECT rowid FROM memory_records WHERE id = ?", (record.id,)
        ).fetchone()
        if existing is not None:
            cur.execute("DELETE FROM memory_vec WHERE rowid = ?", (existing[0],))
            cur.execute("DELETE FROM memory_records WHERE id = ?", (record.id,))
        cur.execute(
            """
            INSERT INTO memory_records
                (id, created_at, topic_tags, severity, description, lesson_text,
                 source_run_id, superseded_by, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.created_at.isoformat(),
                json.dumps(record.topic_tags),
                record.severity.value,
                record.description,
                record.lesson_text,
                record.source_run_id,
                record.superseded_by,
                record.status.value,
            ),
        )
        rowid = cur.lastrowid
        cur.execute(
            "INSERT INTO memory_vec(rowid, embedding) VALUES (?, ?)",
            (rowid, sqlite_vec.serialize_float32(embedding)),
        )
        self._conn.commit()
        _log.info("write_memory", id=record.id, topic_tags=record.topic_tags)
        return record

    def query_memory(
        self, query: str, top_k: int = 3, include_archived: bool = False
    ) -> list[ScoredMemory]:
        """Retrieve the top-k records by the §6.1 composite score.

        Args:
            query: Free-text query.
            top_k: Maximum records to return (architecture.md §6.1 default 3).
            include_archived: If True, archived records are eligible (audit
                trail, architecture.md §6.3); default False.

        Returns:
            Up to ``top_k`` :class:`ScoredMemory`, highest score first.
        """
        now = datetime.now(tz=UTC)
        (count,) = self._conn.execute("SELECT count(*) FROM memory_vec").fetchone()
        if count == 0:
            return []
        query_vec = sqlite_vec.serialize_float32(self._embedder.embed(query))
        # k must be a literal in sqlite-vec's KNN; count is a trusted int.
        neighbours = self._conn.execute(
            "SELECT rowid, distance FROM memory_vec "
            f"WHERE embedding MATCH ? AND k = {int(count)} ORDER BY distance",
            (query_vec,),
        ).fetchall()

        scored: list[ScoredMemory] = []
        for rowid, distance in neighbours:
            row = self._conn.execute(
                "SELECT * FROM memory_records WHERE rowid = ?", (rowid,)
            ).fetchone()
            record = _row_to_record(row)
            if not include_archived and record.status is not MemoryStatus.ACTIVE:
                continue
            semantic = max(0.0, 1.0 - float(distance))  # cosine distance -> similarity
            weight = severity_weight(record.severity)
            recency = recency_decay(record.created_at, now)
            scored.append(
                ScoredMemory(
                    record=record,
                    score=semantic * weight * recency,
                    semantic=semantic,
                    severity_weight=weight,
                    recency=recency,
                )
            )
        scored.sort(key=lambda s: s.score, reverse=True)
        return scored[:top_k]

    def decay_memory(self, now: datetime | None = None) -> DecayReport:
        """Archive stale/superseded records (architecture.md §6.3).

        Re-scores every active record: archives those whose recency decay has
        fallen below the threshold, then archives any whose ``topic_tags`` are a
        subset of a newer active record's tags with severity ≤ that record's
        (marking ``superseded_by``).

        Args:
            now: Reference time; defaults to the current UTC time.

        Returns:
            A :class:`DecayReport` summarising what was archived.
        """
        reference = now if now is not None else datetime.now(tz=UTC)
        active = [r for r in self._all_records() if r.status is MemoryStatus.ACTIVE]

        report = DecayReport()
        by_recency = {
            r.id
            for r in active
            if recency_decay(r.created_at, reference) < RECENCY_ARCHIVE_THRESHOLD
        }
        for record_id in by_recency:
            self._set_status(record_id, MemoryStatus.ARCHIVED, superseded_by=None)
        report.archived_by_recency = len(by_recency)

        remaining = [r for r in active if r.id not in by_recency]
        for record in remaining:
            superseder = _find_superseder(record, remaining)
            if superseder is not None:
                self._set_status(
                    record.id, MemoryStatus.ARCHIVED, superseded_by=superseder.id
                )
                report.superseded_pairs.append((record.id, superseder.id))
        report.archived_by_supersession = len(report.superseded_pairs)

        superseded_ids = {pair[0] for pair in report.superseded_pairs}
        report.active_remaining = len(
            [r for r in remaining if r.id not in superseded_ids]
        )
        self._conn.commit()
        _log.info(
            "decay_memory",
            archived_recency=report.archived_by_recency,
            archived_superseded=report.archived_by_supersession,
            active_remaining=report.active_remaining,
        )
        return report

    # -- helpers --------------------------------------------------------------

    def get(self, record_id: str) -> MemoryRecord | None:
        """Return a record by id, or None if absent."""
        row = self._conn.execute(
            "SELECT * FROM memory_records WHERE id = ?", (record_id,)
        ).fetchone()
        return _row_to_record(row) if row is not None else None

    def all_records(self, include_archived: bool = True) -> list[MemoryRecord]:
        """Return all records (optionally only active ones)."""
        records = self._all_records()
        if include_archived:
            return records
        return [r for r in records if r.status is MemoryStatus.ACTIVE]

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()

    def _all_records(self) -> list[MemoryRecord]:
        rows = self._conn.execute(
            "SELECT * FROM memory_records ORDER BY created_at"
        ).fetchall()
        return [_row_to_record(row) for row in rows]

    def _set_status(
        self, record_id: str, status: MemoryStatus, superseded_by: str | None
    ) -> None:
        self._conn.execute(
            "UPDATE memory_records SET status = ?, superseded_by = ? WHERE id = ?",
            (status.value, superseded_by, record_id),
        )


def _find_superseder(
    record: MemoryRecord, pool: list[MemoryRecord]
) -> MemoryRecord | None:
    """Return the newest record in ``pool`` that supersedes ``record`` (§6.3)."""
    tags = set(record.topic_tags)
    candidates = [
        other
        for other in pool
        if other.id != record.id
        and other.created_at > record.created_at
        and tags <= set(other.topic_tags)
        and severity_rank(other.severity) >= severity_rank(record.severity)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda r: r.created_at)


def _row_to_record(row: tuple[object, ...]) -> MemoryRecord:
    """Build a :class:`MemoryRecord` from a ``memory_records`` row."""
    return MemoryRecord(
        id=str(row[0]),
        created_at=datetime.fromisoformat(str(row[1])),
        topic_tags=list(json.loads(str(row[2]))),
        severity=Severity(str(row[3])),
        description=str(row[4]),
        lesson_text=str(row[5]),
        source_run_id=str(row[6]),
        superseded_by=str(row[7]) if row[7] is not None else None,
        status=MemoryStatus(str(row[8])),
    )
