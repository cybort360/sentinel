"""FastMCP server exposing the three MemoryMCP tools (architecture.md §5.3).

Thin wiring over :class:`MemoryStore`; logic and tests live there. Per §6.1 the
Lessons Agent injects only ``lesson_text`` and ``topic_tags`` per round, so
``query_memory`` returns those (plus the score for transparency) rather than full
historical transcripts.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from sentinel.mcp_servers.memory_mcp.config import MemoryConfig
from sentinel.mcp_servers.runtime import run_server
from sentinel.memory.schema import MemoryRecord
from sentinel.memory.store import MemoryStore
from sentinel.orchestrator.schema import Severity

_store: MemoryStore | None = None


def _get_store() -> MemoryStore:
    """Return the lazily-initialised MemoryMCP store."""
    global _store
    if _store is None:
        _store = MemoryStore(MemoryConfig.from_env().db_path)
    return _store


def query_memory(
    query: str, top_k: int = 3, include_archived: bool = False
) -> list[dict[str, Any]]:
    """Retrieve the most relevant prior incidents for a query.

    Ranks by semantic similarity weighted by severity and recency, returning at
    most `top_k` records. Only `lesson_text` and `topic_tags` are meant for
    injection into the War Room (architecture.md §6.1).

    Args:
        query: Free-text description of the current situation.
        top_k: Maximum number of records to return.
        include_archived: Include archived records (audit trail) if True.

    Returns:
        A list of dicts with `id`, `topic_tags`, `lesson_text`, `severity`, and
        the composite `score`, highest score first.
    """
    results = _get_store().query_memory(
        query, top_k=top_k, include_archived=include_archived
    )
    return [
        {
            "id": s.record.id,
            "topic_tags": s.record.topic_tags,
            "lesson_text": s.record.lesson_text,
            "severity": s.record.severity.value,
            "score": s.score,
        }
        for s in results
    ]


def write_memory(
    topic_tags: list[str],
    severity: str,
    description: str,
    lesson_text: str,
    source_run_id: str,
) -> dict[str, Any]:
    """Write a new post-mortem record to the store.

    Args:
        topic_tags: Non-empty list of topic tags (used for decay/supersession).
        severity: One of "high", "medium", "low".
        description: What was tried.
        lesson_text: The NEW constraint this introduces (not a fix).
        source_run_id: The audit run that produced this record.

    Returns:
        A dict with the stored record's `id` and `created_at`.
    """
    record = MemoryRecord(
        topic_tags=topic_tags,
        severity=Severity(severity),
        description=description,
        lesson_text=lesson_text,
        source_run_id=source_run_id,
    )
    stored = _get_store().write_memory(record)
    return {"id": stored.id, "created_at": stored.created_at.isoformat()}


def decay_memory() -> dict[str, Any]:
    """Re-score the store, archiving stale or superseded records.

    Run at session start (architecture.md §6.3): archives records past their
    relevance horizon or superseded by newer records on the same topics.

    Returns:
        A dict summarising how many records were archived and how.
    """
    return _get_store().decay_memory().model_dump()


def build_server() -> FastMCP:
    """Construct the FastMCP app with all three MemoryMCP tools registered."""
    mcp = FastMCP("sentinel-memory")
    for fn in (query_memory, write_memory, decay_memory):
        mcp.add_tool(fn, description=fn.__doc__)
    return mcp


def main() -> None:
    """Run the MemoryMCP server over the env-selected transport (default stdio)."""
    run_server(build_server())


if __name__ == "__main__":
    main()
