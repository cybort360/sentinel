"""Pydantic model for MemoryMCP post-mortem records (architecture.md §6.2).

Kept in the memory package (not ``orchestrator/schema.py``) per CLAUDE.md §5:
memory records live with the memory subsystem. ``Severity`` is reused from the
orchestrator schema so it has a single definition.

Every record must stay eligible for decay (CLAUDE.md §11 anti-pattern):
``created_at`` is always populated and ``topic_tags`` is required non-empty, so
``decay_memory()`` can always score it (architecture.md §6.3).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from sentinel.orchestrator.schema import Severity


class MemoryStatus(StrEnum):
    """Lifecycle state of a memory record (architecture.md §6.2, §6.3)."""

    ACTIVE = "active"
    ARCHIVED = "archived"


def _utc_now() -> datetime:
    """Return the current UTC time (factory for ``created_at``)."""
    return datetime.now(tz=UTC)


class MemoryRecord(BaseModel):
    """A post-mortem / operational-incident record (architecture.md §6.2).

    ``lesson_text`` is framed as a *new constraint discovered the hard way*, not
    a fix — this is the Track 1 differentiator (architecture.md §4.4). A
    retrieved record should be able to make the Arbitrator's job harder.
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    created_at: datetime = Field(default_factory=_utc_now)
    topic_tags: list[str]
    severity: Severity
    description: str
    lesson_text: str
    source_run_id: str
    superseded_by: str | None = None
    status: MemoryStatus = MemoryStatus.ACTIVE

    @field_validator("topic_tags")
    @classmethod
    def _check_topic_tags(cls, value: list[str]) -> list[str]:
        """Reject records that decay scoring cannot bucket (CLAUDE.md §11)."""
        if not value:
            raise ValueError(
                "topic_tags must be non-empty — decay_memory() relies on it "
                "for supersession (architecture.md §6.3)"
            )
        if any(not tag.strip() for tag in value):
            raise ValueError("topic_tags may not contain empty strings")
        return value


class ScoredMemory(BaseModel):
    """A retrieved record with its §6.1 composite score and components.

    The components are surfaced for transparency and testing — the final
    ``score`` is ``semantic * severity_weight * recency`` (architecture.md §6.1).
    """

    record: MemoryRecord
    score: float
    semantic: float
    severity_weight: float
    recency: float


class DecayReport(BaseModel):
    """Summary of a ``decay_memory()`` pass (architecture.md §6.3)."""

    archived_by_recency: int = 0
    archived_by_supersession: int = 0
    active_remaining: int = 0
    superseded_pairs: list[tuple[str, str]] = Field(default_factory=list)
