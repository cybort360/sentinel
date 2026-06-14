"""Unit tests for the §10 structured logger + demo-trace rendering.

Proves the key §10 property: the bracketed demo tags are *rendered from* the same
structured events — the tag is a ``demo_tag`` field, the message is the
``summary``, and there is no separate narration string.
"""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from sentinel.observability.trace_logger import (
    DemoTag,
    TraceEvent,
    emit,
    get_logger,
    render_trace_line,
)


def test_renders_bracketed_line_from_a_structured_event() -> None:
    event = {
        "summary": "reentrancy persists under fee spike",
        "demo_tag": "ADVERSARY VETO",
        "agent": "adversary",
        "action": "veto",
        "trace_id": "sim-revert-1",
    }
    assert render_trace_line(event) == (
        "[ADVERSARY VETO] reentrancy persists under fee spike "
        "(agent=adversary action=veto trace_id=sim-revert-1)"
    )


@pytest.mark.parametrize("tag", list(DemoTag))
def test_every_demo_tag_renders_with_its_bracket(tag: DemoTag) -> None:
    line = render_trace_line({"summary": "s", "demo_tag": tag.value})
    assert line.startswith(f"[{tag.value}] s")


def test_the_required_tags_exist() -> None:
    assert {t.value for t in DemoTag} == {
        "SYSTEM DECISION",
        "ADVERSARY VETO",
        "ARBITRATION",
        "CONSTRAINTS UNSATISFIED",
        "ANNOTATED RESIDUAL RISK",
        "HUMAN CHECKPOINT",
    }


def test_untagged_event_renders_as_summary_plus_context() -> None:
    line = render_trace_line({"summary": "tool_call", "action": "measure_gas"})
    assert not line.startswith("[")  # no bracket for a non-demo event
    assert "tool_call" in line and "action=measure_gas" in line


def test_render_falls_back_to_raw_event_key() -> None:
    # Raw structlog dicts (pre-rename) carry the message under ``event``.
    assert render_trace_line({"event": "hi", "demo_tag": "SYSTEM DECISION"}) == (
        "[SYSTEM DECISION] hi"
    )


def test_emit_logs_structured_fields_not_a_narration_string() -> None:
    with capture_logs() as logs:
        emit(
            get_logger("test"),
            DemoTag.SYSTEM_DECISION,
            "run start",
            run_id="r1",
            agent="orchestrator",
            action="run_start",
        )
    (event,) = logs
    # The message is the summary; the tag is a field, NOT baked into the text.
    assert event["event"] == "run start"
    assert event["demo_tag"] == "SYSTEM DECISION"
    assert event["action"] == "run_start" and event["run_id"] == "r1"
    # And the very same event renders to the bracketed demo line.
    assert render_trace_line(event).startswith("[SYSTEM DECISION] run start")


def test_trace_event_schema_accepts_an_emitted_event() -> None:
    event = {
        "ts": "2026-06-12T00:00:00+00:00",
        "summary": "consensus reached on patch patch-1",
        "run_id": "r1",
        "agent": "orchestrator",
        "action": "consensus",
        "trace_id": "sim-1",
        "tokens_used": 0,
        "model": None,
        "demo_tag": "SYSTEM DECISION",
        "level": "info",  # extra field tolerated
    }
    parsed = TraceEvent.model_validate(event)
    assert parsed.summary == "consensus reached on patch patch-1"
    assert parsed.trace_id == "sim-1"
    assert parsed.demo_tag == "SYSTEM DECISION"
