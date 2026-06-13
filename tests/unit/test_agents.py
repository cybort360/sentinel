"""Unit tests for the five SENTINEL agents (architecture.md §4).

Each agent's decision logic is exercised with a scripted fake ChatClient and
fake MCP engines — no network, no real Anvil/SQLite. The fakes stand in for the
"mocked MCP responses": a tool call is dispatched to a fake engine method, whose
canned result is fed back to the (scripted) model, which then emits the agent's
typed decision. The load-bearing properties under test are the Golden-Rule-1
trace_id requirement on the Adversary, and the §6.1 "constraints not solutions"
framing on the Lessons Agent.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from sentinel.agents import (
    AdversaryAgent,
    Arbitrator,
    BaselineAgent,
    LessonsAgent,
    YieldAgent,
)
from sentinel.agents.base import AgentOutputError, ChatResponse, ToolCall
from sentinel.memory.schema import MemoryRecord, ScoredMemory
from sentinel.orchestrator.schema import (
    AdversaryReview,
    AgentRole,
    BaselineAudit,
    LessonsContext,
    Outcome,
    RiskProfile,
    Severity,
    Veto,
    YieldAssessment,
    YieldVerdict,
)


class FakeChatClient:
    """Returns queued ChatResponses and records each call (no network)."""

    def __init__(self, responses: list[ChatResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages, model, tools=None):  # type: ignore[no-untyped-def]
        self.calls.append({"model": model, "tools": tools})
        return self._responses.pop(0)


class FakeCodebase:
    """Stands in for CodebaseEngine; records calls, returns canned dicts."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def read_contract(self, path: str) -> dict[str, Any]:
        """Return a canned contract summary."""
        self.calls.append(("read_contract", {"path": path}))
        return {"path": path, "functions": ["processBilling", "cancelSubscription"]}

    def list_functions(self, path: str) -> dict[str, Any]:
        """Return canned function signatures."""
        self.calls.append(("list_functions", {"path": path}))
        return {"functions": ["processBilling(uint256)", "cancelSubscription()"]}

    def propose_patch(self, path: str, new_content: str) -> dict[str, Any]:
        """Return a canned staged-patch id."""
        self.calls.append(("propose_patch", {"path": path}))
        return {"patch_id": "patch-1", "branch": "sentinel/patch-1"}

    def diff_patch(self, patch_id: str) -> dict[str, Any]:
        """Return a canned unified diff."""
        self.calls.append(("diff_patch", {"patch_id": patch_id}))
        return {"patch_id": patch_id, "diff": "--- a\n+++ b\n"}


class FakeSimulation:
    """Stands in for SimulationEngine; every result carries a trace_id."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def deploy_to_fork(
        self, contract: str, constructor_args: list[str]
    ) -> dict[str, Any]:
        """Return a canned deploy result."""
        self.calls.append(("deploy_to_fork", {"contract": contract}))
        return {"trace_id": "sim-deploy-1", "address": "0xabc"}

    def run_tx_spike(self, address: str, scenario: str) -> dict[str, Any]:
        """Return a canned spike result."""
        self.calls.append(("run_tx_spike", {"scenario": scenario}))
        return {"trace_id": "sim-spike-1", "scenario": scenario}

    def measure_gas(
        self,
        address: str,
        function: str,
        args: list[str],
        value: int = 0,
        sender: str | None = None,
    ) -> dict[str, Any]:
        """Return a canned gas measurement."""
        self.calls.append(("measure_gas", {"function": function}))
        return {"trace_id": "sim-gas-1", "gas_used": 35963, "reverted": False}

    def get_revert_rate(self, address: str, scenario: str, n: int) -> dict[str, Any]:
        """Return a canned 100%-revert result under the fee spike."""
        self.calls.append(("get_revert_rate", {"scenario": scenario, "n": n}))
        return {"trace_id": "sim-revert-1", "scenario": scenario, "revert_rate": 1.0}

    def reset_fork(self) -> dict[str, Any]:
        """Return a canned reset acknowledgement."""
        self.calls.append(("reset_fork", {}))
        return {"trace_id": "sim-reset-1", "ok": True}


class FakeMemoryStore:
    """Stands in for MemoryStore; returns one batching-lesson record."""

    def __init__(self) -> None:
        self.written: list[MemoryRecord] = []
        self.record = MemoryRecord(
            id="mem-batching-1",
            topic_tags=["batching", "settlement-latency"],
            severity=Severity.HIGH,
            description="atomic settlement loop reverted en masse under a fee spike",
            lesson_text=(
                "Batching settlements caps finality: any fix must hold under N "
                "blocks or it recreates the mass-revert incident."
            ),
            source_run_id="run-0",
        )

    def query_memory(
        self, query: str, top_k: int = 3, include_archived: bool = False
    ) -> list[ScoredMemory]:
        """Return one canned scored record."""
        return [
            ScoredMemory(
                record=self.record,
                score=0.59,
                semantic=0.7,
                severity_weight=1.0,
                recency=0.84,
            )
        ]

    def write_memory(self, record: MemoryRecord) -> MemoryRecord:
        """Record the write and echo it back."""
        self.written.append(record)
        return record


def _tool_call(name: str, **arguments: Any) -> ChatResponse:
    return ChatResponse(
        content=None,
        tool_calls=[ToolCall(id="1", name=name, arguments=arguments)],
    )


# --------------------------------------------------------------------------- #
# Yield Agent — argues for shipping, must cite specific functions (§4.1)
# --------------------------------------------------------------------------- #


def test_yield_reads_contract_then_returns_assessment() -> None:
    codebase = FakeCodebase()
    payload = {
        "run_id": "r1",
        "iteration": 0,
        "verdict": "accept",
        "rationale": "processBilling is bounded and gas-efficient.",
        "referenced_functions": ["processBilling"],
        "proposed_parameters": {"batch_size": "50"},
    }
    client = FakeChatClient(
        [
            _tool_call("read_contract", path="x.sol"),
            ChatResponse(content=json.dumps(payload)),
        ]
    )
    agent = YieldAgent(
        client=client,
        model="qwen3-coder-plus",
        tools=YieldAgent.default_tools(codebase),
    )
    result = agent.assess("assess x.sol")
    assert isinstance(result, YieldAssessment)
    assert result.verdict is YieldVerdict.ACCEPT
    assert result.referenced_functions == ["processBilling"]
    assert ("read_contract", {"path": "x.sol"}) in codebase.calls


def test_yield_vague_approval_is_rejected() -> None:
    # No referenced_functions -> §4.1 anti-rubber-stamp guard -> AgentOutputError.
    bad = {
        "run_id": "r1",
        "iteration": 0,
        "verdict": "accept",
        "rationale": "looks good to ship",
        "referenced_functions": [],
    }
    client = FakeChatClient(
        [ChatResponse(content=json.dumps(bad)), ChatResponse(content=json.dumps(bad))]
    )
    agent = YieldAgent(client=client, model="m")
    with pytest.raises(AgentOutputError):
        agent.assess("assess x.sol")


def test_yield_allowlist_is_read_only() -> None:
    names = {t.name for t in YieldAgent.default_tools(FakeCodebase())}
    assert names == {"read_contract", "list_functions"}


# --------------------------------------------------------------------------- #
# Adversary Agent — Golden Rule #1: every claim cites a SimulationMCP trace_id
# --------------------------------------------------------------------------- #


def test_adversary_vetoes_on_simulated_revert_rate_with_trace_id() -> None:
    sim, codebase = FakeSimulation(), FakeCodebase()
    payload = {
        "run_id": "r1",
        "iteration": 1,
        "target_proposal_id": "patch-1",
        "vetoed": True,
        "severity": "high",
        "reason": "fee spike causes total settlement failure",
        "evidence": "get_revert_rate returned 100% under fee_spike",
        "trace_ids": ["sim-revert-1"],
    }
    client = FakeChatClient(
        [
            _tool_call("get_revert_rate", address="0xabc", scenario="fee_spike", n=20),
            ChatResponse(content=json.dumps(payload)),
        ]
    )
    agent = AdversaryAgent(
        client=client,
        model="qwen3-max-thinking",
        tools=AdversaryAgent.default_tools(sim, codebase),
    )
    review = agent.review("evaluate patch-1")
    assert isinstance(review, AdversaryReview)
    assert review.vetoed and review.severity is Severity.HIGH
    assert review.trace_ids == ["sim-revert-1"]
    assert ("get_revert_rate", {"scenario": "fee_spike", "n": 20}) in sim.calls
    # The review materialises into a formal Veto carrying the same trace.
    veto = review.to_veto("veto-1")
    assert isinstance(veto, Veto)
    assert veto.issued_by is AgentRole.ADVERSARY
    assert veto.trace_ids == ["sim-revert-1"]


def test_adversary_unsourced_veto_fails_to_parse() -> None:
    # Empty trace_ids -> Rule 1 violation -> ValidationError -> AgentOutputError.
    bad = {
        "run_id": "r1",
        "iteration": 1,
        "target_proposal_id": "patch-1",
        "vetoed": True,
        "severity": "high",
        "reason": "i just feel like it reverts",
        "evidence": "vibes",
        "trace_ids": [],
    }
    client = FakeChatClient(
        [ChatResponse(content=json.dumps(bad)), ChatResponse(content=json.dumps(bad))]
    )
    agent = AdversaryAgent(client=client, model="m")
    with pytest.raises(AgentOutputError):
        agent.review("evaluate patch-1")


def test_adversary_clearance_still_cites_traces_and_has_no_severity() -> None:
    payload = {
        "run_id": "r1",
        "iteration": 2,
        "target_proposal_id": "patch-2",
        "vetoed": False,
        "reason": "guard holds; revert rate 0% post-patch",
        "evidence": "get_revert_rate returned 0.0 under fee_spike",
        "trace_ids": ["sim-revert-9"],
    }
    client = FakeChatClient([ChatResponse(content=json.dumps(payload))])
    review = AdversaryAgent(client=client, model="m").review("evaluate patch-2")
    assert not review.vetoed and review.severity is None
    assert review.trace_ids == ["sim-revert-9"]  # clearance is also a sim claim
    with pytest.raises(ValueError, match="only valid on a vetoed review"):
        review.to_veto("veto-x")


def test_adversary_allowlist_covers_simulation_plus_read() -> None:
    names = {
        t.name for t in AdversaryAgent.default_tools(FakeSimulation(), FakeCodebase())
    }
    assert {
        "deploy_to_fork",
        "run_tx_spike",
        "measure_gas",
        "get_revert_rate",
        "reset_fork",
    } <= names


# --------------------------------------------------------------------------- #
# Arbitrator — stages patches (never applies), emits a trace-backed RiskProfile
# --------------------------------------------------------------------------- #


def test_arbitrator_proposes_patch_then_emits_risk_profile() -> None:
    codebase = FakeCodebase()
    payload = {
        "run_id": "r1",
        "outcome": "consensus",
        "final_proposal": "patch-1",
        "residual_risk_pct": 0.04,
        "residual_risk_description": "reentrancy guard adds 12.9% gas; revert 0%",
        "mitigations_applied": ["nonReentrant guard on cancelSubscription"],
        "memory_records_used": ["mem-batching-1"],
        "iterations": 2,
        "tokens_total": 18234,
        "trace_ids": ["sim-revert-9", "sim-gas-1"],
    }
    client = FakeChatClient(
        [
            _tool_call("propose_patch", path="x.sol", new_content="// guarded"),
            ChatResponse(content=json.dumps(payload)),
        ]
    )
    agent = Arbitrator(
        client=client, model="qwen3-max", tools=Arbitrator.default_tools(codebase)
    )
    profile = agent.synthesize("synthesize run")
    assert isinstance(profile, RiskProfile)
    assert profile.outcome is Outcome.CONSENSUS
    assert profile.final_proposal == "patch-1"
    assert profile.trace_ids == ["sim-revert-9", "sim-gas-1"]
    assert ("propose_patch", {"path": "x.sol"}) in codebase.calls


def test_arbitrator_cannot_apply_patches() -> None:
    # apply_patch is Human-Checkpoint-gated (§5.1/§7) and must not be bound.
    names = {t.name for t in Arbitrator.default_tools(FakeCodebase())}
    assert names == {"propose_patch", "diff_patch"}
    assert "apply_patch" not in names


def test_arbitrator_riskprofile_without_trace_ids_fails() -> None:
    bad = {
        "run_id": "r1",
        "outcome": "consensus",
        "residual_risk_pct": 0.0,
        "residual_risk_description": "all clear (but no evidence cited)",
        "iterations": 1,
        "tokens_total": 10,
        "trace_ids": [],
    }
    client = FakeChatClient(
        [ChatResponse(content=json.dumps(bad)), ChatResponse(content=json.dumps(bad))]
    )
    with pytest.raises(AgentOutputError):
        Arbitrator(client=client, model="m").synthesize("synthesize run")


# --------------------------------------------------------------------------- #
# Lessons Agent — frames retrieved memory as constraints, not solutions (§6.1)
# --------------------------------------------------------------------------- #


def test_lessons_query_tool_returns_only_injectable_fields() -> None:
    # §6.1: only lesson_text + topic_tags (plus id/severity/score) are injected,
    # never the full historical transcript (`description`).
    store = FakeMemoryStore()
    query_tool = next(
        t for t in LessonsAgent.default_tools(store) if t.name == "query_memory"
    )
    rows = query_tool.call({"query": "fee spike mass revert"})
    assert set(rows[0]) == {"id", "topic_tags", "lesson_text", "severity", "score"}
    assert "description" not in rows[0]


def test_lessons_injects_constraint_not_a_fix() -> None:
    store = FakeMemoryStore()
    payload = {
        "constraints": [
            {
                "constraint_text": (
                    "Any fix must hold finality under N blocks — batching caps "
                    "settlement latency."
                ),
                "topic_tags": ["batching", "settlement-latency"],
                "source_memory_id": "mem-batching-1",
            }
        ],
        "memory_records_used": ["mem-batching-1"],
    }
    client = FakeChatClient(
        [
            _tool_call("query_memory", query="fee spike mass revert"),
            ChatResponse(content=json.dumps(payload)),
        ]
    )
    agent = LessonsAgent(
        client=client, model="qwen-plus", tools=LessonsAgent.default_tools(store)
    )
    context = agent.recall("veto: settlement reverts under fee spike")
    assert isinstance(context, LessonsContext)
    assert context.memory_records_used == ["mem-batching-1"]
    assert context.constraints[0].source_memory_id == "mem-batching-1"
    # The injected text is a limitation ("must"), not a recipe.
    assert "must" in context.constraints[0].constraint_text.lower()


def test_lessons_empty_recall_is_allowed() -> None:
    client = FakeChatClient(
        [
            ChatResponse(
                content=json.dumps({"constraints": [], "memory_records_used": []})
            )
        ]
    )
    context = LessonsAgent(client=client, model="m").recall("nothing relevant")
    assert context.constraints == []


# --------------------------------------------------------------------------- #
# Baseline Agent — single-pass control, no simulation, no memory (§4.5)
# --------------------------------------------------------------------------- #


def test_baseline_produces_single_pass_audit() -> None:
    codebase = FakeCodebase()
    payload = {
        "vulnerabilities": ["reentrancy in cancelSubscription"],
        "recommended_fix": "add a nonReentrant guard",
        "referenced_functions": ["cancelSubscription"],
        "residual_risk_disclosed": False,
    }
    client = FakeChatClient(
        [
            _tool_call("read_contract", path="x.sol"),
            ChatResponse(content=json.dumps(payload)),
        ]
    )
    agent = BaselineAgent(
        client=client, model="qwen3-max", tools=BaselineAgent.default_tools(codebase)
    )
    audit = agent.audit("audit x.sol")
    assert isinstance(audit, BaselineAudit)
    assert audit.vulnerabilities == ["reentrancy in cancelSubscription"]


def test_baseline_has_no_simulation_access() -> None:
    names = {t.name for t in BaselineAgent.default_tools(FakeCodebase())}
    assert names == {"read_contract", "list_functions"}
    assert not (names & {"get_revert_rate", "run_tx_spike", "deploy_to_fork"})
