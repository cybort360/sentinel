"""Live War Room assembly: real Qwen agents wired to the MCP engines (§4, §8).

This is the seam between the LLM agents and the orchestration graph. ``demo/``
wires the deterministic demo drivers; this builds the *real* War Room — the five
Qwen-backed agents, each with its §4 tool allowlist (`default_tools`) and model
id (from :class:`QwenConfig`) — into a :class:`WarRoomGraph`. It is the body of
``run_demo --live`` / ``make dev`` and is exercised offline by the mocked-client
smoke test, so the wiring is verified without Qwen access; only the live model
call is deferred until credentials exist.
"""

from __future__ import annotations

from sentinel.agents.adversary_agent import AdversaryAgent
from sentinel.agents.arbitrator import Arbitrator
from sentinel.agents.base import ChatClient
from sentinel.agents.baseline_agent import BaselineAgent
from sentinel.agents.lessons_agent import LessonsAgent
from sentinel.agents.qwen_client import QwenConfig
from sentinel.agents.yield_agent import YieldAgent
from sentinel.mcp_servers.codebase_mcp.engine import CodebaseEngine
from sentinel.mcp_servers.simulation_mcp.engine import SimulationEngine
from sentinel.memory.store import MemoryStore
from sentinel.orchestrator.graph import WarRoomGraph
from sentinel.orchestrator.schema import AgentRole
from sentinel.orchestrator.token_budget import BudgetConfig


def build_war_room(
    *,
    client: ChatClient,
    config: QwenConfig,
    simulation: SimulationEngine,
    codebase: CodebaseEngine,
    memory: MemoryStore,
    budget: BudgetConfig | None = None,
    with_baseline: bool = True,
) -> WarRoomGraph:
    """Assemble the live War Room from real agents bound to the MCP engines.

    Each agent gets its §4 model id from ``config`` and its tool allowlist from
    the corresponding engine (Yield/Baseline: CodebaseMCP read; Adversary:
    SimulationMCP + CodebaseMCP read; Arbitrator: CodebaseMCP patch staging;
    Lessons: MemoryMCP). The graph is unchanged — these agents satisfy the same
    Protocols the demo drivers do, so live and demo share one orchestration path.

    Args:
        client: The LLM client (real :class:`QwenClient` or a test fake).
        config: Per-agent model ids + Qwen connection settings.
        simulation: SimulationMCP engine (the Adversary's evidence source).
        codebase: CodebaseMCP engine (read + patch staging).
        memory: MemoryMCP store (the Lessons Agent's institutional memory).
        budget: §9 budget knobs; defaults to ``BudgetConfig.from_env()``.
        with_baseline: Include the Baseline control agent for the §11 comparison.

    Returns:
        A :class:`WarRoomGraph` ready to ``run`` a contract audit.
    """
    yield_agent = YieldAgent(
        client=client,
        model=config.model_for(AgentRole.YIELD),
        tools=YieldAgent.default_tools(codebase),
    )
    adversary = AdversaryAgent(
        client=client,
        model=config.model_for(AgentRole.ADVERSARY),
        tools=AdversaryAgent.default_tools(simulation, codebase),
    )
    arbitrator = Arbitrator(
        client=client,
        model=config.model_for(AgentRole.ARBITRATOR),
        tools=Arbitrator.default_tools(codebase),
    )
    lessons = LessonsAgent(
        client=client,
        model=config.model_for(AgentRole.LESSONS),
        tools=LessonsAgent.default_tools(memory),
    )
    baseline = (
        BaselineAgent(
            client=client,
            model=config.model_for(AgentRole.BASELINE),
            tools=BaselineAgent.default_tools(codebase),
        )
        if with_baseline
        else None
    )
    return WarRoomGraph(
        yield_agent=yield_agent,
        adversary=adversary,
        arbitrator=arbitrator,
        lessons=lessons,
        codebase=codebase,
        budget=budget,
        baseline=baseline,
    )
