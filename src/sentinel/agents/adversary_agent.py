"""Adversary Agent, "the Brakes" (architecture.md §4.2).

Finds execution flaws, systemic risk, and cost anomalies, and holds veto power.
Its credibility rests entirely on Golden Rule #1: every claim — veto *or*
clearance — is backed by a real ``SimulationMCP`` run, cited by ``trace_id``.
That requirement is enforced structurally by ``AdversaryReview`` (non-empty
``trace_ids``), not just by the prompt.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from sentinel.agents.base import AgentBase, ChatClient, Tool, tool_from_callable
from sentinel.mcp_servers.codebase_mcp.engine import CodebaseEngine
from sentinel.mcp_servers.simulation_mcp.engine import SimulationEngine
from sentinel.orchestrator.schema import AdversaryReview, AgentRole


class AdversaryAgent(AgentBase):
    """The evidence-backed risk veto (architecture.md §4.2)."""

    PROMPT = "adversary"

    def __init__(
        self,
        *,
        client: ChatClient,
        model: str,
        tools: list[Tool] | None = None,
        prompts_dir: Path | None = None,
        max_tool_iterations: int = 12,
        max_output_repairs: int = 1,
    ) -> None:
        """Configure the Adversary Agent.

        Args:
            client: The LLM client (real ``QwenClient`` or a test fake).
            model: Model id (``qwen3-max`` by default, from config/.env).
            tools: SimulationMCP + CodebaseMCP read tools; build via
                :meth:`default_tools`.
            prompts_dir: Override the prompts directory (tests).
            max_tool_iterations: Cap on tool rounds (higher — it simulates).
            max_output_repairs: Re-asks allowed on an unparseable reply.
        """
        super().__init__(
            role=AgentRole.ADVERSARY,
            client=client,
            model=model,
            system_prompt=AgentBase.load_prompt(self.PROMPT, prompts_dir),
            tools=tools,
            output_schema=AdversaryReview,
            max_tool_iterations=max_tool_iterations,
            max_output_repairs=max_output_repairs,
        )

    @staticmethod
    def default_tools(
        simulation: SimulationEngine, codebase: CodebaseEngine
    ) -> list[Tool]:
        """Build the allowlist: all SimulationMCP tools + CodebaseMCP read."""
        return [
            tool_from_callable(simulation.deploy_to_fork),
            tool_from_callable(simulation.run_tx_spike),
            tool_from_callable(simulation.measure_gas),
            tool_from_callable(simulation.get_revert_rate),
            tool_from_callable(simulation.reset_fork),
            tool_from_callable(codebase.read_contract),
            tool_from_callable(codebase.list_functions),
        ]

    def review(self, task: str) -> AdversaryReview:
        """Run the agent and return its typed veto/clearance decision."""
        return cast(AdversaryReview, self.run(task))
