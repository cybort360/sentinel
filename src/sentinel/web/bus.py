"""In-process async pub/sub for the live trace stream (architecture.md §18).

The orchestrator emits §10 events from whatever thread it runs on (the War Room
offloads ``graph.run`` to an executor), while the browsers consuming them live on
the asyncio event loop. :class:`TraceBus` bridges the two: :meth:`publish` is
safe to call from any thread, fan-out and delivery always happen on the loop, and
a bounded replay buffer lets a browser that connects mid-run catch up.

The bus carries plain dicts tagged with a ``kind`` so a single SSE stream can
multiplex trace events (``kind="trace"``) and control events
(``checkpoint_pending``, ``run_complete``, …). It never holds a second copy of
the audit's truth — trace events are verbatim copies of what the §10 logger
already rendered.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncGenerator
from typing import Any

Event = dict[str, Any]


class TraceBus:
    """Thread-safe fan-out of trace/control events to SSE subscribers."""

    def __init__(self, *, replay_size: int = 2000) -> None:
        """Create an unbound bus.

        Args:
            replay_size: How many recent events to retain for back-filling a
                late-joining subscriber.
        """
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._replay: deque[Event] = deque(maxlen=replay_size)
        # Monotonic per-event id. Replayed events keep their original ``_seq`` so a
        # reconnecting client can drop anything it has already rendered — this is
        # what stops EventSource auto-reconnects from duplicating the timeline.
        self._seq = 0

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the event loop that owns delivery (called at app startup)."""
        self._loop = loop

    def publish(self, event: Event) -> None:
        """Publish an event from any thread (delivery hops to the loop)."""
        loop = self._loop
        if loop is None:
            # Pre-startup: no live subscribers yet, just retain for replay.
            self._replay.append(self._stamp(event))
            return
        loop.call_soon_threadsafe(self._publish_on_loop, event)

    def _stamp(self, event: Event) -> Event:
        """Attach a monotonic ``_seq`` so replay is idempotent for clients."""
        self._seq += 1
        return {**event, "_seq": self._seq}

    def _publish_on_loop(self, event: Event) -> None:
        """Fan an event out to every subscriber (runs on the loop thread)."""
        stamped = self._stamp(event)
        self._replay.append(stamped)
        for queue in self._subscribers:
            queue.put_nowait(stamped)

    async def subscribe(self) -> AsyncGenerator[Event, None]:
        """Yield events as they arrive, after replaying the recent backlog.

        Yields:
            Each event dict, oldest retained first, then live events forever
            (until the consumer disconnects).
        """
        queue: asyncio.Queue[Event] = asyncio.Queue()
        for event in list(self._replay):
            queue.put_nowait(event)
        self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        """Return the number of currently connected subscribers."""
        return len(self._subscribers)
