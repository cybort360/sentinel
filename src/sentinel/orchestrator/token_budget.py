"""Token & model budget orchestration (architecture.md §9).

§9's claim is that compute scales with how *contentious* an audit actually is,
not with a fixed pipeline length. Three of its four mechanisms are control-flow
rules enforced by the graph (architecture.md §8) using the values here:

* **Iteration cap** — ``max_iterations`` (default 4) hard-bounds the War Room
  loop.
* **Early exit** — a clean initial scan skips the loop entirely (§9), which the
  graph detects and the :class:`TokenLedger` makes visible (few entries).
* **Context windowing** — ``memory_top_k`` (default 3) bounds how many memory
  records the Lessons Agent injects per round (§6.1).

The fourth mechanism, **model tiering**, is realised by each agent's configured
model id (architecture.md §9 table / §13); the :class:`TokenLedger` records the
model each step used so the §10 observability trail shows the tiering in action.
Per-call token *counts* are threaded in once the LLM client surfaces usage; until
then entries record 0 and ``total`` is a lower bound — callers should treat it as
such, never as a fabricated figure.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

_DEFAULT_MAX_ITERATIONS = 4
_DEFAULT_MEMORY_TOP_K = 3


@dataclass(frozen=True)
class BudgetConfig:
    """The §9 budget knobs (read from ``.env`` per CLAUDE.md §9)."""

    max_iterations: int = _DEFAULT_MAX_ITERATIONS
    memory_top_k: int = _DEFAULT_MEMORY_TOP_K

    @classmethod
    def from_env(cls) -> BudgetConfig:
        """Build the budget from ``MAX_ITERATIONS`` / ``MEMORY_TOP_K`` (.env)."""
        return cls(
            max_iterations=int(
                os.environ.get("MAX_ITERATIONS", str(_DEFAULT_MAX_ITERATIONS))
            ),
            memory_top_k=int(
                os.environ.get("MEMORY_TOP_K", str(_DEFAULT_MEMORY_TOP_K))
            ),
        )

    def __post_init__(self) -> None:
        """Reject nonsensical budgets early (a 0-iteration loop never runs)."""
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be >= 1")
        if self.memory_top_k < 1:
            raise ValueError("memory_top_k must be >= 1")


@dataclass
class LedgerEntry:
    """One accounted agent action (doubles as the §10 observability row)."""

    agent: str
    action: str
    model: str
    tokens: int = 0
    trace_id: str | None = None


@dataclass
class TokenLedger:
    """Accumulates per-step token/model accounting for a single run.

    Feeds ``RiskProfile.tokens_total`` and the §10 trail. ``total`` sums whatever
    token counts were supplied; with usage threading not yet wired it is a lower
    bound, not an invented number.
    """

    entries: list[LedgerEntry] = field(default_factory=list)

    def record(
        self,
        agent: str,
        action: str,
        model: str,
        *,
        tokens: int = 0,
        trace_id: str | None = None,
    ) -> None:
        """Append one accounted step to the ledger."""
        self.entries.append(
            LedgerEntry(
                agent=agent,
                action=action,
                model=model,
                tokens=tokens,
                trace_id=trace_id,
            )
        )

    @property
    def total(self) -> int:
        """Lower-bound total tokens consumed so far this run."""
        return sum(e.tokens for e in self.entries)

    @property
    def steps(self) -> int:
        """Number of accounted agent actions (a contention proxy, §9)."""
        return len(self.entries)
