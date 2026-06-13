"""Baseline Agent, single-pass comparison control (architecture.md §4.5).

Exists only for the Track 3 efficiency comparison (§11): one shot, CodebaseMCP
read access only, no simulation, no memory, no negotiation. Runs the same
flagship model as the Arbitrator so the comparison isolates the multi-agent
architecture's effect, not raw model capability.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from sentinel.agents.base import AgentBase, ChatClient, Tool, tool_from_callable
from sentinel.mcp_servers.codebase_mcp.engine import CodebaseEngine
from sentinel.orchestrator.schema import AgentRole, BaselineAudit


class BaselineAgent(AgentBase):
    """Single-pass audit control for the efficiency table (§4.5, §11)."""

    PROMPT = "baseline"

    def __init__(
        self,
        *,
        client: ChatClient,
        model: str,
        tools: list[Tool] | None = None,
        prompts_dir: Path | None = None,
        max_tool_iterations: int = 4,
        max_output_repairs: int = 1,
    ) -> None:
        """Configure the Baseline Agent.

        Args:
            client: The LLM client (real ``QwenClient`` or a test fake).
            model: Model id (``qwen3-max`` per §4.5 — same as the Arbitrator).
            tools: CodebaseMCP read tools; use :meth:`default_tools`.
            prompts_dir: Override the prompts directory (tests).
            max_tool_iterations: Cap on tool-call rounds (single-pass: low).
            max_output_repairs: Re-asks allowed on an unparseable reply.
        """
        super().__init__(
            role=AgentRole.BASELINE,
            client=client,
            model=model,
            system_prompt=AgentBase.load_prompt(self.PROMPT, prompts_dir),
            tools=tools,
            output_schema=BaselineAudit,
            max_tool_iterations=max_tool_iterations,
            max_output_repairs=max_output_repairs,
        )

    @staticmethod
    def default_tools(codebase: CodebaseEngine) -> list[Tool]:
        """Build the allowlist: CodebaseMCP read access only (no simulation)."""
        return [
            tool_from_callable(codebase.read_contract),
            tool_from_callable(codebase.list_functions),
        ]

    def audit(self, task: str) -> BaselineAudit:
        """Run the agent and return its typed single-pass audit."""
        return cast(BaselineAudit, self.run(task))
