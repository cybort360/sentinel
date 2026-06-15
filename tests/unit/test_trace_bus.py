"""TraceBus fan-out/replay and the trace_logger sink tap (architecture.md §18)."""

from __future__ import annotations

import asyncio

from structlog.testing import capture_logs

from sentinel.observability.trace_logger import get_logger, register_sink
from sentinel.web import TraceBus


def test_bus_delivers_live_events() -> None:
    asyncio.run(_delivers_live())


async def _delivers_live() -> None:
    bus = TraceBus()
    bus.bind_loop(asyncio.get_running_loop())
    stream = bus.subscribe()
    bus.publish({"kind": "trace", "summary": "hello"})
    event = await asyncio.wait_for(stream.__anext__(), timeout=1.0)
    assert event["summary"] == "hello"
    await stream.aclose()


def test_replay_keeps_stable_seq_so_reconnects_dont_duplicate() -> None:
    asyncio.run(_stable_seq())


async def _stable_seq() -> None:
    # Events get a monotonic _seq; replay preserves it, so a reconnecting client
    # tracking the highest _seq it has seen drops the re-sent backlog (the fix for
    # the endless-scroll duplication on EventSource auto-reconnect).
    bus = TraceBus()
    bus.bind_loop(asyncio.get_running_loop())
    bus.publish({"kind": "trace", "summary": "a"})
    bus.publish({"kind": "trace", "summary": "b"})
    await asyncio.sleep(0.02)  # let call_soon_threadsafe deliver to the replay buffer

    async def replayed_seqs() -> list[int]:
        stream = bus.subscribe()
        out = [
            (await asyncio.wait_for(stream.__anext__(), timeout=1.0))["_seq"]
            for _ in range(2)
        ]
        await stream.aclose()
        return out

    first = await replayed_seqs()
    second = await replayed_seqs()  # a "reconnect"
    assert first == [1, 2]
    assert second == first  # same seqs -> a client with lastSeq=2 skips them all


def test_bus_replays_backlog_to_late_subscriber() -> None:
    asyncio.run(_replays_backlog())


async def _replays_backlog() -> None:
    bus = TraceBus()
    bus.bind_loop(asyncio.get_running_loop())
    bus.publish({"kind": "trace", "summary": "earlier"})  # before anyone subscribes
    stream = bus.subscribe()
    event = await asyncio.wait_for(stream.__anext__(), timeout=1.0)
    assert event["summary"] == "earlier"
    await stream.aclose()


def test_register_sink_receives_emitted_events() -> None:
    received: list[dict[str, object]] = []
    unregister = register_sink(lambda event: received.append(dict(event)))
    try:
        # capture_logs swaps the processor chain, so also assert via the sink:
        log = get_logger("test-bus")
        log.info("a structured event", trace_id="abc123")
    finally:
        unregister()

    assert any(
        e.get("summary") == "a structured event" and e.get("trace_id") == "abc123"
        for e in received
    ), received

    # After unregister, no further events are captured.
    before = len(received)
    get_logger("test-bus").info("after unregister")
    assert len(received) == before


def test_capture_logs_still_works_alongside_sinks() -> None:
    # Sanity: the fanout processor must not break structlog's test capture.
    with capture_logs() as logs:
        get_logger("test-bus").info("captured", action="x")
    assert any(entry.get("event") == "captured" for entry in logs)
