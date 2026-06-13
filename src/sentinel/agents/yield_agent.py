"""Yield Agent, "the Accelerator" (architecture.md §4.1).

Argues *for* shipping; maximizes throughput and time-to-deploy. Has no veto
power and makes no simulation claims — its allowlist is CodebaseMCP read access
only, and its output must cite specific functions (the §4.1 guard against
rubber-stamping is enforced structurally by ``YieldAssessment``).
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from sentinel.agents.base import AgentBase, ChatClient, Tool, tool_from_callable
from sentinel.mcp_servers.codebase_mcp.engine import CodebaseEngine
from sentinel.orchestrator.schema import AgentRole, YieldAssessment


class YieldAgent(AgentBase):
    """The throughput/time-to-deploy advocate (architecture.md §4.1)."""

    PROMPT = "yield"

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
        """Configure the Yield Agent.

        Args:
            client: The LLM client (real ``QwenClient`` or a test fake).
            model: Model id (``qwen3-coder-plus`` per §4.1, from config/.env).
            tools: CodebaseMCP read tools; use :meth:`default_tools` to build.
            prompts_dir: Override the prompts directory (tests).
            max_tool_iterations: Cap on tool-call rounds.
            max_output_repairs: Re-asks allowed on an unparseable reply.
        """
        super().__init__(
            role=AgentRole.YIELD,
            client=client,
            model=model,
            system_prompt=AgentBase.load_prompt(self.PROMPT, prompts_dir),
            tools=tools,
            output_schema=YieldAssessment,
            max_tool_iterations=max_tool_iterations,
            max_output_repairs=max_output_repairs,
        )

    @staticmethod
    def default_tools(codebase: CodebaseEngine) -> list[Tool]:
        """Build the Yield Agent's allowlist: CodebaseMCP read access only."""
        return [
            tool_from_callable(codebase.read_contract),
            tool_from_callable(codebase.list_functions),
        ]

    def assess(self, task: str) -> YieldAssessment:
        """Run the agent and return its typed shipping assessment."""
        return cast(YieldAssessment, self.run(task))
