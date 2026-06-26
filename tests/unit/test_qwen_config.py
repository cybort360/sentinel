"""Unit tests for QwenConfig env parsing (CLAUDE.md §9). No network."""

from __future__ import annotations

import pytest

from sentinel.agents.qwen_client import QwenConfig, QwenConfigError
from sentinel.orchestrator.schema import AgentRole


def test_from_env_reads_models_and_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QWEN_API_KEY", "sk-test")
    monkeypatch.setenv("QWEN_BASE_URL", "https://example.invalid/compatible-mode/v1")
    monkeypatch.setenv("QWEN_MODEL_YIELD", "custom-yield-model")
    monkeypatch.delenv("QWEN_MODEL_ARBITRATOR", raising=False)  # falls back to draft

    config = QwenConfig.from_env()

    assert config.base_url.endswith("/v1")
    assert config.model_for(AgentRole.YIELD) == "custom-yield-model"
    # Unset model var falls back to the documented draft default.
    assert config.model_for(AgentRole.ARBITRATOR) == "qwen3-max"
    assert config.model_for(AgentRole.ADVERSARY) == "qwen3-max"


def test_from_env_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    monkeypatch.setenv("QWEN_BASE_URL", "https://example.invalid/v1")
    with pytest.raises(QwenConfigError):
        QwenConfig.from_env()
