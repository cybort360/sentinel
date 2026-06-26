"""Thin OpenAI-compatible Qwen Cloud client (architecture.md §13, CLAUDE.md §9).

Qwen Cloud (Alibaba Model Studio / DashScope) exposes an OpenAI-compatible
endpoint, so this is a small wrapper around the ``openai`` SDK pointed at
``QWEN_BASE_URL``. Per-agent model ids and the base URL are read from the
environment — nothing is hardcoded, so once the values are confirmed against the
hackathon voucher credentials they only need to change in ``.env``.

The draft model ids (``qwen3-max``, ``qwen3-coder-plus``, ``qwen-plus``)
and the base URL are UNVERIFIED placeholders. Run this
module as a script with real credentials to verify them::

    uv run python -m sentinel.agents.qwen_client
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from openai import OpenAI

from sentinel.agents.base import ChatResponse, ToolCall
from sentinel.observability.trace_logger import get_logger
from sentinel.orchestrator.schema import AgentRole

_log = get_logger("qwen")

# (env var, draft default) per agent. Defaults match CLAUDE.md §9 / .env.example
# and are explicitly UNVERIFIED — confirm against the voucher catalog.
_MODEL_ENV: dict[AgentRole, tuple[str, str]] = {
    AgentRole.YIELD: ("QWEN_MODEL_YIELD", "qwen3-coder-plus"),
    AgentRole.ADVERSARY: ("QWEN_MODEL_ADVERSARY", "qwen3-max"),
    AgentRole.ARBITRATOR: ("QWEN_MODEL_ARBITRATOR", "qwen3-max"),
    AgentRole.LESSONS: ("QWEN_MODEL_LESSONS", "qwen-plus"),
    AgentRole.BASELINE: ("QWEN_MODEL_BASELINE", "qwen3-max"),
}


class QwenConfigError(RuntimeError):
    """Raised when required Qwen credentials are missing from the environment."""


@dataclass(frozen=True)
class QwenConfig:
    """Qwen Cloud connection settings and per-agent model ids."""

    api_key: str
    base_url: str
    models: dict[AgentRole, str]

    @classmethod
    def from_env(cls) -> QwenConfig:
        """Build a config from the environment (see CLAUDE.md §9).

        Raises:
            QwenConfigError: If ``QWEN_API_KEY`` or ``QWEN_BASE_URL`` is unset
                (these have no safe default — confirm the region-correct base URL
                from the voucher credentials).
        """
        api_key = os.environ.get("QWEN_API_KEY")
        base_url = os.environ.get("QWEN_BASE_URL")
        if not api_key or not base_url:
            raise QwenConfigError(
                "QWEN_API_KEY and QWEN_BASE_URL must be set (from the Qwen Cloud "
                "voucher credentials) — see .env.example / CLAUDE.md §9"
            )
        models = {
            role: os.environ.get(var, default)
            for role, (var, default) in _MODEL_ENV.items()
        }
        return cls(api_key=api_key, base_url=base_url, models=models)

    def model_for(self, role: AgentRole) -> str:
        """Return the configured model id for an agent role."""
        return self.models[role]


class QwenClient:
    """A thin OpenAI-compatible chat client for Qwen Cloud."""

    def __init__(self, config: QwenConfig) -> None:
        """Create the client against the configured Qwen Cloud endpoint."""
        self._config = config
        self._client = OpenAI(api_key=config.api_key, base_url=config.base_url)

    def chat(
        self,
        messages: list[dict[str, object]],
        model: str,
        tools: list[dict[str, object]] | None = None,
    ) -> ChatResponse:
        """Run one chat completion (implements the ``ChatClient`` protocol).

        Args:
            messages: OpenAI-format message list.
            model: Model id to call.
            tools: OpenAI-format tool schemas, if the agent has any bound.

        Returns:
            The model's turn as a :class:`ChatResponse`.
        """
        extra: dict[str, object] = {"tools": tools} if tools else {}
        completion = self._client.chat.completions.create(
            model=model, messages=messages, **extra
        )
        message = completion.choices[0].message
        content = message.content if isinstance(message.content, str) else None
        tool_calls: list[ToolCall] = []
        for call in getattr(message, "tool_calls", None) or []:
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except (json.JSONDecodeError, TypeError):
                arguments = {}
            tool_calls.append(
                ToolCall(
                    id=str(call.id),
                    name=str(call.function.name),
                    arguments=dict(arguments),
                )
            )
        return ChatResponse(content=content, tool_calls=tool_calls)

    def verify_models(self) -> dict[str, bool]:
        """Ping each configured model with a 1-token request; report reachability.

        Returns:
            A mapping of model id to whether the call succeeded. Failures are
            logged ``[DEGRADED]`` (Rule 4), never raised — so one bad model id
            does not hide the others.
        """
        results: dict[str, bool] = {}
        for model in sorted(set(self._config.models.values())):
            try:
                self._client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=1,
                )
                results[model] = True
                _log.info("model_ok", model=model)
            except Exception as exc:  # noqa: BLE001 — report each model independently
                results[model] = False
                _log.error("[DEGRADED] model check failed", model=model, error=str(exc))
        return results


def main() -> None:
    """Verify the configured Qwen models against live credentials."""
    config = QwenConfig.from_env()
    _log.info(
        "qwen_config",
        base_url=config.base_url,
        models={role.value: model for role, model in config.models.items()},
    )
    results = QwenClient(config).verify_models()
    _log.info("verify_complete", results=results, all_ok=all(results.values()))


if __name__ == "__main__":
    main()
