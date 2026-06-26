"""Unit tests for the §9 budget config and token ledger."""

from __future__ import annotations

import pytest

from sentinel.orchestrator.token_budget import BudgetConfig, TokenLedger


def test_from_env_reads_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_ITERATIONS", "6")
    monkeypatch.setenv("MEMORY_TOP_K", "5")
    budget = BudgetConfig.from_env()
    assert budget.max_iterations == 6
    assert budget.memory_top_k == 5


def test_from_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAX_ITERATIONS", raising=False)
    monkeypatch.delenv("MEMORY_TOP_K", raising=False)
    budget = BudgetConfig.from_env()
    assert budget.max_iterations == 4  # architecture.md §9 default
    assert budget.memory_top_k == 3


def test_rejects_degenerate_budget() -> None:
    with pytest.raises(ValueError, match="max_iterations"):
        BudgetConfig(max_iterations=0)
    with pytest.raises(ValueError, match="memory_top_k"):
        BudgetConfig(memory_top_k=0)


def test_ledger_totals_and_steps() -> None:
    ledger = TokenLedger()
    ledger.record("adversary", "simulate", "qwen3-max", tokens=120, trace_id="t1")
    ledger.record("yield", "evaluate", "qwen3-coder-plus", tokens=30)
    ledger.record("lessons", "recall", "qwen-plus")  # unknown usage -> 0
    assert ledger.total == 150  # lower bound — unknown counts contribute 0
    assert ledger.steps == 3
    assert ledger.entries[0].trace_id == "t1"
