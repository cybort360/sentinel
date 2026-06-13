"""Lessons Agent, "Memory" (architecture.md §4.4).

Owns ``MemoryMCP``. Retrieves prior incidents and injects them as *constraints
discovered the hard way* — never as ready-made fixes (the Track 1 differentiator,
§6.1). Also writes the end-of-run post-mortem. Only ``lesson_text`` and
``topic_tags`` are injected per round (§6.1), so the bound query tool returns
exactly those fields, not full transcripts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from sentinel.agents.base import AgentBase, ChatClient, Tool, tool_from_callable
from sentinel.memory.schema import MemoryRecord
from sentinel.memory.store import MemoryStore
from sentinel.orchestrator.schema import AgentRole, LessonsContext, Severity


class LessonsAgent(AgentBase):
    """Retrieves and reframes prior incidents as live constraints (§4.4)."""

    PROMPT = "lessons"

    def __init__(
        self,
        *,
        client: ChatClient,
        model: str,
        tools: list[Tool] | None = None,
        prompts_dir: Path | None = None,
        max_tool_iterations: int = 6,
        max_output_repairs: int = 1,
    ) -> None:
        """Configure the Lessons Agent.

        Args:
            client: The LLM client (real ``QwenClient`` or a test fake).
            model: Model id (``qwen-plus`` per §4.4 — cheap, called often).
            tools: MemoryMCP query/write tools; build via :meth:`default_tools`.
            prompts_dir: Override the prompts directory (tests).
            max_tool_iterations: Cap on tool-call rounds.
            max_output_repairs: Re-asks allowed on an unparseable reply.
        """
        super().__init__(
            role=AgentRole.LESSONS,
            client=client,
            model=model,
            system_prompt=AgentBase.load_prompt(self.PROMPT, prompts_dir),
            tools=tools,
            output_schema=LessonsContext,
            max_tool_iterations=max_tool_iterations,
            max_output_repairs=max_output_repairs,
        )

    @staticmethod
    def default_tools(memory: MemoryStore) -> list[Tool]:
        """Build the allowlist: MemoryMCP query + write, bound to ``memory``.

        The query tool returns only ``lesson_text``/``topic_tags`` (plus id,
        severity, score) per §6.1 — never full historical transcripts.
        """

        def query_memory(query: str, top_k: int = 3) -> list[dict[str, Any]]:
            """Retrieve ≤top_k prior incidents relevant to the current situation.

            Returns each record's lesson_text and topic_tags (the only fields
            meant for injection, architecture.md §6.1) with its composite score.
            """
            return [
                {
                    "id": s.record.id,
                    "topic_tags": s.record.topic_tags,
                    "lesson_text": s.record.lesson_text,
                    "severity": s.record.severity.value,
                    "score": s.score,
                }
                for s in memory.query_memory(query, top_k=top_k)
            ]

        def write_memory(
            topic_tags: list[str],
            severity: str,
            description: str,
            lesson_text: str,
            source_run_id: str,
        ) -> dict[str, str]:
            """Write a post-mortem; lesson_text is a NEW constraint, not a fix."""
            record = memory.write_memory(
                MemoryRecord(
                    topic_tags=topic_tags,
                    severity=Severity(severity),
                    description=description,
                    lesson_text=lesson_text,
                    source_run_id=source_run_id,
                )
            )
            return {"id": record.id, "created_at": record.created_at.isoformat()}

        return [tool_from_callable(query_memory), tool_from_callable(write_memory)]

    def recall(self, task: str) -> LessonsContext:
        """Run the agent and return its typed constraint injection."""
        return cast(LessonsContext, self.run(task))
