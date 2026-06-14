"""Offline smoke test for the live (Qwen) War Room path (architecture.md §4, §8).

Proves the real agent classes + ``build_war_room`` + the orchestration graph run
end to end with a *scripted fake* ChatClient — no Qwen, no network, no Anvil. The
fake returns valid structured JSON per requested schema (detected from the
schema directive the agent appends to its system prompt), so each real agent
parses a real ``YieldAssessment`` / ``AdversaryReview`` / ``Proposal`` /
``RiskProfile`` / ``LessonsContext`` / ``BaselineAudit`` and the graph reaches a
``RiskProfile``. When Qwen credentials exist, ``QwenClient`` drops into the same
seam — only the live model call is deferred, the wiring is verified here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sentinel.agents.base import ChatResponse
from sentinel.agents.factory import build_war_room
from sentinel.agents.qwen_client import QwenConfig
from sentinel.mcp_servers.codebase_mcp.config import CodebaseConfig
from sentinel.mcp_servers.codebase_mcp.engine import CodebaseEngine
from sentinel.memory.store import MemoryStore
from sentinel.orchestrator.graph import RunResult
from sentinel.orchestrator.schema import AgentRole, Outcome
from sentinel.orchestrator.token_budget import BudgetConfig

_REPO_ROOT = Path(__file__).resolve().parents[2]

# One canned, schema-valid reply per agent output. trace_ids are placeholders —
# in a live run they come from real SimulationMCP calls; the graph only requires
# them to be present (Rule 1's structural check), which is what we exercise here.
_REPLIES: dict[str, dict[str, Any]] = {
    "YieldAssessment": {
        "run_id": "smoke",
        "iteration": 1,
        "verdict": "accept",
        "rationale": "processBilling is bounded",
        "referenced_functions": ["processBilling"],
    },
    "AdversaryReview": {
        "run_id": "smoke",
        "iteration": 1,
        "target_proposal_id": "orig",
        "vetoed": True,
        "severity": "high",
        "reason": "reverts under fee spike",
        "evidence": "get_revert_rate = 1.0 (5/5)",
        "trace_ids": ["t-adv"],
    },
    "Proposal": {
        "proposal_id": "p1",
        "run_id": "smoke",
        "iteration": 1,
        "patch_id": "guard",
        "summary": "add a nonReentrant guard",
        "trace_ids": ["t-prop"],
    },
    "RiskProfile": {
        "run_id": "smoke",
        "outcome": "constraints_unsatisfied",
        "residual_risk_pct": 0.04,
        "residual_risk_description": "batching vs latency tension remains",
        "mitigations_applied": ["circuit breaker"],
        "iterations": 2,
        "tokens_total": 0,
        "trace_ids": ["t-risk"],
    },
    "LessonsContext": {"constraints": [], "memory_records_used": []},
    "BaselineAudit": {
        "recommended_fix": "add a nonReentrant guard",
        "vulnerabilities": ["reentrancy"],
        "referenced_functions": ["cancelSubscription"],
        "residual_risk_disclosed": False,
    },
}


class _FakeChat:
    """A ChatClient that returns canned JSON for whichever schema is requested."""

    def __init__(self) -> None:
        self.calls = 0

    def chat(
        self,
        messages: list[dict[str, Any]],
        model: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        self.calls += 1
        system = str(messages[0]["content"])
        for name, payload in _REPLIES.items():
            if f'"title": "{name}"' in system:
                return ChatResponse(content=json.dumps(payload))
        return ChatResponse(content="{}")


class _FakeSim:
    """Stand-in for SimulationEngine.

    The methods exist so the agent's tool binding can introspect them, but the
    fake LLM never issues a tool call, so they are never executed.
    """

    def deploy_to_fork(self, contract: str, constructor_args: list) -> object:
        raise NotImplementedError

    def run_tx_spike(self, address: str, scenario: str) -> object:
        raise NotImplementedError

    def measure_gas(
        self,
        address: str,
        function: str,
        args: list,
        value: int = 0,
        sender: str | None = None,
    ) -> object:
        raise NotImplementedError

    def get_revert_rate(self, address: str, scenario: str, n: int = 10) -> object:
        raise NotImplementedError

    def reset_fork(self) -> object:
        raise NotImplementedError


def _config() -> QwenConfig:
    return QwenConfig(
        api_key="test",
        base_url="https://example.invalid/v1",
        models={role: f"m-{role.value}" for role in AgentRole},
    )


def test_build_war_room_wires_each_agent_to_its_model() -> None:
    graph = build_war_room(
        client=_FakeChat(),
        config=_config(),
        simulation=_FakeSim(),  # type: ignore[arg-type]
        codebase=CodebaseEngine(CodebaseConfig(repo_root=_REPO_ROOT)),
        memory=MemoryStore(":memory:"),
    )
    assert graph._yield.model == "m-yield"
    assert graph._adversary.model == "m-adversary"
    assert graph._arbitrator.model == "m-arbitrator"
    assert graph._lessons.model == "m-lessons"
    assert graph._baseline is not None and graph._baseline.model == "m-baseline"


def test_live_war_room_runs_end_to_end_offline() -> None:
    client = _FakeChat()
    memory = MemoryStore(":memory:")
    graph = build_war_room(
        client=client,
        config=_config(),
        simulation=_FakeSim(),  # type: ignore[arg-type]
        codebase=CodebaseEngine(CodebaseConfig(repo_root=_REPO_ROOT)),
        memory=memory,
        budget=BudgetConfig(max_iterations=2, memory_top_k=1),
    )
    try:
        result = graph.run("sandbox/contracts/SubscriptionBilling.sol", run_id="smoke")
    finally:
        memory.close()

    assert isinstance(result, RunResult)
    assert result.risk_profile.outcome is Outcome.CONSTRAINTS_UNSATISFIED
    assert result.risk_profile.trace_ids  # the run produced a sourced profile
    assert client.calls > 0  # the real agents actually drove the fake model
