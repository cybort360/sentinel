"""Orchestration: graph, token budget, checkpoint, schemas (architecture.md §8).

Re-exported symbols are resolved **lazily** (PEP 562 ``__getattr__``) rather than
imported at package load. Eagerly importing ``checkpoint``/``graph`` here created
an import cycle — ``memory.schema`` imports ``orchestrator.schema``, which runs
this ``__init__``, which (eagerly) imported ``checkpoint``, which imports back
into the still-initialising ``memory.schema``. That only bites a process that
imports ``memory.schema`` first, e.g. starting MemoryMCP cold. Lazy access keeps
the ergonomic ``from sentinel.orchestrator import WarRoomGraph`` API without the
cycle; direct submodule imports (``from sentinel.orchestrator.graph import ...``)
are unaffected.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sentinel.orchestrator.checkpoint import (
        CheckpointDecision,
        CheckpointOutcome,
        CheckpointResponse,
        ConsoleResponder,
        DecisionPacket,
        HumanCheckpoint,
        MemoryStorePostMortemWriter,
        build_decision_packet,
        gated_reasons,
    )
    from sentinel.orchestrator.graph import RunResult, WarRoomGraph
    from sentinel.orchestrator.token_budget import BudgetConfig, TokenLedger

# Each lazily-exported name -> the submodule that defines it.
_LAZY: dict[str, str] = {
    "CheckpointDecision": "checkpoint",
    "CheckpointOutcome": "checkpoint",
    "CheckpointResponse": "checkpoint",
    "ConsoleResponder": "checkpoint",
    "DecisionPacket": "checkpoint",
    "HumanCheckpoint": "checkpoint",
    "MemoryStorePostMortemWriter": "checkpoint",
    "build_decision_packet": "checkpoint",
    "gated_reasons": "checkpoint",
    "RunResult": "graph",
    "WarRoomGraph": "graph",
    "BudgetConfig": "token_budget",
    "TokenLedger": "token_budget",
}

__all__ = [
    "BudgetConfig",
    "CheckpointDecision",
    "CheckpointOutcome",
    "CheckpointResponse",
    "ConsoleResponder",
    "DecisionPacket",
    "HumanCheckpoint",
    "MemoryStorePostMortemWriter",
    "RunResult",
    "TokenLedger",
    "WarRoomGraph",
    "build_decision_packet",
    "gated_reasons",
]


def __getattr__(name: str) -> Any:
    """Lazily import a re-exported orchestrator symbol on first access (PEP 562)."""
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"{__name__}.{module}"), name)
    globals()[name] = value  # cache so subsequent lookups skip __getattr__
    return value


def __dir__() -> list[str]:
    """List the lazily-exported names alongside the module's own globals."""
    return sorted(set(globals()) | set(__all__))
