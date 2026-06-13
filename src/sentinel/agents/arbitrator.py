"""Arbitrator, "the Synthesizer" (architecture.md §4.3).

A mostly-mechanical constraint solver: tracks the active proposal and the
Yield/Adversary positions, stages patches via ``CodebaseMCP.propose_patch``, and
always produces the run's ``RiskProfile``. It rejects any veto lacking a
``trace_id`` and never fabricates consensus — the ``RiskProfile`` schema enforces
that its residual-risk numbers cite real ``SimulationMCP`` runs (Rule 1).
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from sentinel.agents.base import AgentBase, ChatClient, Tool, tool_from_callable
from sentinel.mcp_servers.codebase_mcp.engine import CodebaseEngine
from sentinel.orchestrator.schema import AgentRole, Proposal, RiskProfile


class Arbitrator(AgentBase):
    """The constraint-solving synthesizer that emits the RiskProfile (§4.3)."""

    PROMPT = "arbitrator"

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
        """Configure the Arbitrator.

        Args:
            client: The LLM client (real ``QwenClient`` or a test fake).
            model: Model id (``qwen3-max`` per §4.3, from config/.env).
            tools: CodebaseMCP patch-staging tools; build via
                :meth:`default_tools`.
            prompts_dir: Override the prompts directory (tests).
            max_tool_iterations: Cap on tool-call rounds.
            max_output_repairs: Re-asks allowed on an unparseable reply.
        """
        super().__init__(
            role=AgentRole.ARBITRATOR,
            client=client,
            model=model,
            system_prompt=AgentBase.load_prompt(self.PROMPT, prompts_dir),
            tools=tools,
            output_schema=RiskProfile,
            max_tool_iterations=max_tool_iterations,
            max_output_repairs=max_output_repairs,
        )

    @staticmethod
    def default_tools(codebase: CodebaseEngine) -> list[Tool]:
        """Build the allowlist: stage patches and read their diffs (not apply).

        ``apply_patch`` is deliberately excluded — applying a patch is gated by
        the Human Checkpoint (architecture.md §5.1/§7), never the Arbitrator.
        """
        return [
            tool_from_callable(codebase.propose_patch),
            tool_from_callable(codebase.diff_patch),
        ]

    def propose(self, task: str) -> Proposal:
        """Stage a candidate patch for this round and return it as a Proposal.

        Mid-loop the Arbitrator synthesizes the Yield/Adversary positions into a
        patch (via ``propose_patch``); the structured output is a ``Proposal``,
        whose ``trace_ids`` must cite the simulation runs motivating it (Rule 1).
        """
        return cast(Proposal, self.run(task, output_schema=Proposal))

    def synthesize(self, task: str) -> RiskProfile:
        """Run the agent and return its typed run-terminating RiskProfile."""
        return cast(RiskProfile, self.run(task))
