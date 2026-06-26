from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from demo.web_demo import _run_uploaded_live

from sentinel.agents.base import ChatResponse
from sentinel.agents.qwen_client import QwenConfig
from sentinel.mcp_servers.codebase_mcp.config import CodebaseConfig
from sentinel.mcp_servers.codebase_mcp.engine import CodebaseEngine
from sentinel.memory.store import MemoryStore
from sentinel.orchestrator.checkpoint import CheckpointDecision, CheckpointResponse
from sentinel.orchestrator.schema import AgentRole
from sentinel.web import TraceBus

_REPO_ROOT = Path(__file__).resolve().parents[2]


class _FakeChat:
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
        if '"title": "YieldAssessment"' in system:
            return ChatResponse(
                content=json.dumps(
                    {
                        "run_id": "upload-subscriptionbilling",
                        "iteration": 1,
                        "verdict": "accept",
                        "rationale": "processBilling is bounded",
                        "referenced_functions": ["processBilling"],
                    }
                )
            )
        if '"title": "AdversaryReview"' in system:
            return ChatResponse(
                content=json.dumps(
                    {
                        "run_id": "upload-subscriptionbilling",
                        "iteration": 1,
                        "target_proposal_id": "orig",
                        "vetoed": True,
                        "severity": "high",
                        "reason": "reverts under fee spike",
                        "evidence": "get_revert_rate = 1.0",
                        "trace_ids": ["sim-upload"],
                    }
                )
            )
        if '"title": "Proposal"' in system:
            return ChatResponse(
                content=json.dumps(
                    {
                        "proposal_id": "p1",
                        "run_id": "upload-subscriptionbilling",
                        "iteration": 1,
                        "patch_id": "guard",
                        "summary": "add guard",
                        "trace_ids": ["sim-upload"],
                    }
                )
            )
        if '"title": "RiskProfile"' in system:
            return ChatResponse(
                content=json.dumps(
                    {
                        "run_id": "upload-subscriptionbilling",
                        "outcome": "constraints_unsatisfied",
                        "residual_risk_pct": 0.05,
                        "residual_risk_description": "manual review required",
                        "mitigations_applied": ["guard"],
                        "iterations": 1,
                        "tokens_total": 0,
                        "trace_ids": ["sim-upload"],
                    }
                )
            )
        if '"title": "LessonsContext"' in system:
            return ChatResponse(content=json.dumps({"constraints": []}))
        if '"title": "BaselineAudit"' in system:
            return ChatResponse(
                content=json.dumps(
                    {
                        "vulnerabilities": ["reentrancy"],
                        "recommended_fix": "add guard",
                        "referenced_functions": ["cancelSubscription"],
                        "residual_risk_disclosed": False,
                    }
                )
            )
        return ChatResponse(content="{}")


class _FakeSimulation:
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


class _ApproveResponder:
    async def ask(self, packet: object) -> CheckpointResponse:
        return CheckpointResponse(
            decision=CheckpointDecision.APPROVE,
            rationale="test approval",
            responded_at=datetime.now(tz=UTC),
        )


def _config() -> QwenConfig:
    return QwenConfig(
        api_key="test",
        base_url="https://example.invalid/v1",
        models={role: "qwen3-max" for role in AgentRole},
    )


def test_uploaded_contract_runs_live_war_room_when_qwen_is_configured() -> None:
    asyncio.run(_uploaded_live())


async def _uploaded_live() -> None:
    client = _FakeChat()
    memory = MemoryStore(":memory:")
    bus = TraceBus()
    bus.bind_loop(asyncio.get_running_loop())
    try:
        await _run_uploaded_live(
            "sandbox/contracts/SubscriptionBilling.sol",
            "SubscriptionBilling",
            simulation=_FakeSimulation(),
            codebase=CodebaseEngine(CodebaseConfig(repo_root=_REPO_ROOT)),
            memory=memory,
            responder=_ApproveResponder(),
            bus=bus,
            live=(client, _config()),
        )
        await asyncio.sleep(0)
        run_complete = None
        events = bus.subscribe()
        try:
            async for event in events:
                if event.get("kind") == "run_complete":
                    run_complete = event
                    break
        finally:
            await events.aclose()
    finally:
        memory.close()

    assert client.calls > 0
    assert run_complete is not None
    assert run_complete["residual_risk_description"]
    assert "Final Adversary veto remains unresolved" in str(
        run_complete["residual_risk_description"]
    )
