"""Structured logging + demo-trace rendering for SENTINEL (architecture.md §10).

All agent and orchestrator output flows through structlog — never ``print``
(CLAUDE.md §5). Every event carries the §10 schema fields (``ts``, ``run_id``,
``agent``, ``action``, ``trace_id``, ``summary``, ``tokens_used``, ``model``).

The demo-facing bracketed tags (``[ADVERSARY VETO]``, ``[CONSTRAINTS
UNSATISFIED]``, ``[SYSTEM DECISION]``, ``[ANNOTATED RESIDUAL RISK]``,
``[HUMAN CHECKPOINT]``) are **rendered from these same structured events** — the
tag is a structured ``demo_tag`` field, not a hand-written string, and the
bracketed line is produced by :func:`render_trace_line` from the event dict.
There is therefore a single event stream: JSON output (``mode="json"``) is the
debug log, the demo renderer (``mode="demo"``) is the trace, and both come from
the exact same emitted events. No separate narration path exists.

Emit a demo-tagged event with :func:`emit`; everything else uses a plain
structlog logger from :func:`get_logger`.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Any, Literal, cast

import structlog
from pydantic import BaseModel, ConfigDict
from structlog.typing import EventDict, WrappedLogger

_configured = False

#: Process-wide side-channel taps. Each receives a *copy* of every fully
#: processed event dict, in addition to the terminal renderer — so an alternate
#: consumer (e.g. the web UI's live trace, src/sentinel/web) reads the exact same
#: §10 event stream, never a second narration path. Best-effort: a misbehaving
#: sink is isolated and never breaks logging.
_SINKS: list[Callable[[Mapping[str, Any]], None]] = []


class DemoTag(StrEnum):
    """The five demo-trace tags rendered from structured events (§10)."""

    SYSTEM_DECISION = "SYSTEM DECISION"
    ADVERSARY_VETO = "ADVERSARY VETO"
    CONSTRAINTS_UNSATISFIED = "CONSTRAINTS UNSATISFIED"
    ANNOTATED_RESIDUAL_RISK = "ANNOTATED RESIDUAL RISK"
    HUMAN_CHECKPOINT = "HUMAN CHECKPOINT"


class TraceEvent(BaseModel):
    """The architecture.md §10 structured-log schema (one emitted event).

    Documents and (in tests) validates the shape every event conforms to. Extra
    keys are permitted — events may carry event-specific context beyond the core
    schema (e.g. ``iteration``) and structlog adds ``level``.
    """

    model_config = ConfigDict(extra="allow")

    ts: str
    run_id: str | None = None
    agent: str | None = None
    action: str | None = None
    trace_id: str | None = None
    summary: str
    tokens_used: int = 0
    model: str | None = None
    demo_tag: str | None = None


def render_trace_line(event: Mapping[str, Any]) -> str:
    """Render one structured event as a demo-trace line (architecture.md §10).

    A ``demo_tag`` event becomes ``[TAG] summary (agent=… action=… trace_id=…)``;
    an untagged event renders as its summary plus key/value context, so the demo
    renderer never loses information and never needs a second narration path.

    Args:
        event: The structured event dict (post-rename, so the message is under
            ``summary``; ``event`` is also accepted for raw structlog dicts).

    Returns:
        The single-line rendering.
    """
    summary = str(event.get("summary", event.get("event", "")))
    tag = event.get("demo_tag")
    context = " ".join(
        f"{key}={event[key]}"
        for key in ("agent", "action", "trace_id")
        if event.get(key) not in (None, "")
    )
    if tag:
        head = f"[{tag}] {summary}"
        return f"{head} ({context})" if context else head
    return f"{summary} {context}".rstrip() if context else summary


def emit(
    logger: structlog.typing.FilteringBoundLogger,
    tag: DemoTag,
    summary: str,
    *,
    run_id: str | None = None,
    agent: str | None = None,
    action: str | None = None,
    trace_id: str | None = None,
    tokens_used: int = 0,
    model: str | None = None,
    **extra: Any,
) -> None:
    """Emit a demo-tagged §10 event (rendered bracketed or as JSON, same event).

    Args:
        logger: The bound logger to emit through.
        tag: Which demo tag this event renders under.
        summary: The human-readable one-liner (the §10 ``summary`` field).
        run_id: The audit run id.
        agent: Emitting agent/component.
        action: Machine-readable action label.
        trace_id: The ``SimulationMCP`` trace backing any claim (Golden Rule #1).
        tokens_used: Tokens consumed by the step, if known.
        model: Model id, if the step called one.
        **extra: Event-specific context.
    """
    logger.info(
        summary,
        demo_tag=tag.value,
        run_id=run_id,
        agent=agent,
        action=action,
        trace_id=trace_id,
        tokens_used=tokens_used,
        model=model,
        **extra,
    )


def register_sink(sink: Callable[[Mapping[str, Any]], None]) -> Callable[[], None]:
    """Tap the event stream: ``sink`` is called with every processed event.

    The sink sees the same dict the terminal renderer does (it runs just before
    rendering), so it never invents events. Intended for alternate live
    consumers such as the web UI (architecture.md §10, §18).

    Args:
        sink: Callable invoked with a copy of each event dict. Must not raise;
            exceptions are swallowed so logging is never broken by a tap.

    Returns:
        A zero-arg callable that unregisters this sink.
    """
    _SINKS.append(sink)

    def _unregister() -> None:
        with contextlib.suppress(ValueError):
            _SINKS.remove(sink)

    return _unregister


def _fanout_to_sinks(
    logger: WrappedLogger, name: str, event_dict: EventDict
) -> EventDict:
    """Offer each registered sink a copy of the event, then pass it through."""
    if _SINKS:
        snapshot = dict(event_dict)
        for sink in _SINKS:
            # A misbehaving tap must never break logging (best-effort side channel).
            with contextlib.suppress(Exception):
                sink(snapshot)
    return event_dict


def _rename_event_to_summary(
    logger: WrappedLogger, name: str, event_dict: EventDict
) -> EventDict:
    """Rename structlog's ``event`` key to ``summary`` (architecture.md §10)."""
    if "event" in event_dict and "summary" not in event_dict:
        event_dict["summary"] = event_dict.pop("event")
    return event_dict


def _demo_renderer(logger: WrappedLogger, name: str, event_dict: EventDict) -> str:
    """Render an event's demo-trace line (structlog terminal processor)."""
    return render_trace_line(event_dict)


def configure_logging(
    *,
    level: int = logging.INFO,
    mode: Literal["json", "demo", "console"] = "json",
) -> None:
    """Configure structlog process-wide. Idempotent.

    Args:
        level: Minimum log level to emit.
        mode: Terminal renderer for the *same* event stream — ``json`` (the
            §10 schema, the debug log), ``demo`` (bracketed demo trace), or
            ``console`` (dev-friendly key/value).
    """
    global _configured

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    renderer: structlog.typing.Processor
    if mode == "json":
        renderer = structlog.processors.JSONRenderer()
    elif mode == "demo":
        renderer = _demo_renderer
    else:
        renderer = structlog.dev.ConsoleRenderer()

    processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _rename_event_to_summary,
        _fanout_to_sinks,
        renderer,
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None) -> structlog.typing.FilteringBoundLogger:
    """Return a bound structlog logger, configuring logging on first use.

    Args:
        name: Optional logger name, typically the module or agent name.

    Returns:
        A bound logger ready to emit structured events.
    """
    if not _configured:
        configure_logging()
    return cast("structlog.typing.FilteringBoundLogger", structlog.get_logger(name))
