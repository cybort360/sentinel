"""Unit tests for AgentBase: tool loop, output parsing, and tool binding.

Uses a scripted fake ChatClient — no network, fully deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sentinel.agents.base import (
    AgentBase,
    AgentError,
    AgentOutputError,
    ChatResponse,
    ToolCall,
    tool_from_callable,
)
from sentinel.orchestrator.schema import AgentRole, Proposal


class FakeChatClient:
    """Returns queued ChatResponses and records each call."""

    def __init__(self, responses: list[ChatResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def chat(self, messages, model, tools=None):  # type: ignore[no-untyped-def]
        self.calls.append(
            {"messages": [dict(m) for m in messages], "model": model, "tools": tools}
        )
        return self._responses.pop(0)


def _agent(client: FakeChatClient, **kw: object) -> AgentBase:
    return AgentBase(
        role=AgentRole.ARBITRATOR,
        client=client,
        model="m",
        system_prompt="sys",
        **kw,  # type: ignore[arg-type]
    )


def test_plain_text_run_returns_content() -> None:
    client = FakeChatClient([ChatResponse(content="hello world")])
    assert _agent(client).run("hi") == "hello world"


def test_tool_call_is_dispatched_then_final_answer_returned() -> None:
    seen: list[int] = []

    def double(x: int) -> dict[str, int]:
        """Double a number."""
        seen.append(x)
        return {"doubled": x * 2}

    client = FakeChatClient(
        [
            ChatResponse(
                content=None,
                tool_calls=[ToolCall(id="1", name="double", arguments={"x": 21})],
            ),
            ChatResponse(content="the answer is 42"),
        ]
    )
    agent = _agent(client, tools=[tool_from_callable(double)])
    assert agent.run("go") == "the answer is 42"
    assert seen == [21]
    # The second call must include a tool result message correlated by id.
    second_messages = client.calls[1]["messages"]
    assert any(
        m.get("role") == "tool" and m.get("tool_call_id") == "1"
        for m in second_messages  # type: ignore[union-attr]
    )


def test_unknown_tool_does_not_crash_and_run_recovers() -> None:
    client = FakeChatClient(
        [
            ChatResponse(
                content=None, tool_calls=[ToolCall(id="1", name="ghost", arguments={})]
            ),
            ChatResponse(content="recovered"),
        ]
    )
    assert _agent(client).run("go") == "recovered"
    tool_msg = next(m for m in client.calls[1]["messages"] if m.get("role") == "tool")  # type: ignore[union-attr]
    assert "error" in str(tool_msg["content"])


def test_structured_output_parses_into_pydantic_schema() -> None:
    payload = {
        "proposal_id": "p1",
        "run_id": "r1",
        "iteration": 0,
        "summary": "add reentrancy guard",
        "trace_ids": ["sim-1"],
    }
    client = FakeChatClient([ChatResponse(content=json.dumps(payload))])
    result = _agent(client, output_schema=Proposal).run("propose")
    assert isinstance(result, Proposal)
    assert result.trace_ids == ["sim-1"]


def test_structured_output_strips_code_fences() -> None:
    payload = {
        "proposal_id": "p1",
        "run_id": "r1",
        "iteration": 1,
        "summary": "x",
        "trace_ids": ["sim-9"],
    }
    fenced = f"```json\n{json.dumps(payload)}\n```"
    client = FakeChatClient([ChatResponse(content=fenced)])
    result = _agent(client, output_schema=Proposal).run("propose")
    assert isinstance(result, Proposal)


def test_invalid_json_triggers_one_repair_then_succeeds() -> None:
    payload = {
        "proposal_id": "p1",
        "run_id": "r1",
        "iteration": 0,
        "summary": "x",
        "trace_ids": ["sim-1"],
    }
    client = FakeChatClient(
        [
            ChatResponse(content="sorry, not json"),
            ChatResponse(content=json.dumps(payload)),
        ]
    )
    result = _agent(client, output_schema=Proposal).run("propose")
    assert isinstance(result, Proposal)
    assert len(client.calls) == 2  # original + one repair


def test_persistently_invalid_output_raises() -> None:
    client = FakeChatClient(
        [ChatResponse(content="nope"), ChatResponse(content="still nope")]
    )
    with pytest.raises(AgentOutputError):
        _agent(client, output_schema=Proposal).run("propose")


def test_rule1_unsourced_proposal_fails_to_parse() -> None:
    # Empty trace_ids violates Golden Rule #1 -> ValidationError -> AgentOutputError.
    bad = {
        "proposal_id": "p1",
        "run_id": "r1",
        "iteration": 0,
        "summary": "x",
        "trace_ids": [],
    }
    client = FakeChatClient(
        [ChatResponse(content=json.dumps(bad)), ChatResponse(content=json.dumps(bad))]
    )
    with pytest.raises(AgentOutputError):
        _agent(client, output_schema=Proposal).run("propose")


def test_tool_loop_cap_raises() -> None:
    # Always asks for a tool -> never finalizes -> hits the cap.
    forever = [
        ChatResponse(
            content=None, tool_calls=[ToolCall(id=str(i), name="noop", arguments={})]
        )
        for i in range(10)
    ]

    def noop() -> dict[str, bool]:
        """Do nothing."""
        return {"ok": True}

    client = FakeChatClient(forever)
    with pytest.raises(AgentError):
        _agent(client, tools=[tool_from_callable(noop)], max_tool_iterations=3).run(
            "go"
        )


def test_tool_from_callable_builds_schema() -> None:
    def f(a: str, b: int = 3, tags: list[str] | None = None) -> None:
        """A sample tool."""

    tool = tool_from_callable(f)
    props = tool.parameters["properties"]
    assert props["a"] == {"type": "string"}
    assert props["b"] == {"type": "integer"}
    assert props["tags"] == {"type": "array", "items": {"type": "string"}}
    assert tool.parameters["required"] == ["a"]
    assert tool.description == "A sample tool."


def test_load_prompt_reads_file(tmp_path: Path) -> None:
    (tmp_path / "yield.md").write_text("You are the Yield Agent.")
    assert (
        AgentBase.load_prompt("yield", prompts_dir=tmp_path)
        == "You are the Yield Agent."
    )
