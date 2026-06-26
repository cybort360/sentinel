"""Browser-driven Human Checkpoint responder (architecture.md §7, §18).

:class:`WebResponder` implements the same :class:`~sentinel.orchestrator.
checkpoint.HumanResponder` protocol as the CLI ``ConsoleResponder`` — so it slots
into the existing :class:`~sentinel.orchestrator.checkpoint.HumanCheckpoint` with
**zero changes to the gate logic**, and Golden Rule #2 holds by construction.

``ask`` publishes a ``checkpoint_pending`` event (the browser then shows the
panel) and awaits an :class:`asyncio.Future`. The only thing that completes that
future is :meth:`resolve`, called from the ``POST /checkpoint`` handler when a
human clicks a button. There is no default, no timeout, and no auto-approve path
— exactly as in the CLI responder. If no browser ever answers, the run blocks
forever, which is the correct behaviour for a real gate.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sentinel.orchestrator.checkpoint import (
    CheckpointDecision,
    CheckpointResponse,
    DecisionPacket,
    packet_allows_approval,
    packet_allows_incomplete_acknowledgement,
)
from sentinel.web.bus import TraceBus


class CheckpointNotPendingError(RuntimeError):
    """Raised when a decision is submitted for a run with no open gate."""


class WebResponder:
    """A :class:`HumanResponder` whose decision arrives over HTTP, not stdin."""

    def __init__(self, bus: TraceBus) -> None:
        """Wire the responder to the event bus it announces pending gates on."""
        self._bus = bus
        self._pending: dict[str, asyncio.Future[CheckpointResponse]] = {}
        self._packets: dict[str, DecisionPacket] = {}

    async def ask(self, packet: DecisionPacket) -> CheckpointResponse:
        """Announce the gate and block until a browser submits a decision.

        Args:
            packet: The §7.2 Decision Packet to present to the human.

        Returns:
            The human's :class:`CheckpointResponse`, once submitted.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[CheckpointResponse] = loop.create_future()
        self._pending[packet.run_id] = future
        self._packets[packet.run_id] = packet
        self._bus.publish(
            {
                "kind": "checkpoint_pending",
                "run_id": packet.run_id,
                "packet": packet.model_dump(mode="json"),
            }
        )
        try:
            return await future  # genuinely blocks — only resolve() completes it
        finally:
            self._pending.pop(packet.run_id, None)
            self._packets.pop(packet.run_id, None)

    def is_pending(self, run_id: str) -> bool:
        """Return True while ``run_id`` is blocked awaiting a human decision."""
        future = self._pending.get(run_id)
        return future is not None and not future.done()

    def resolve(
        self,
        run_id: str,
        decision: CheckpointDecision,
        *,
        rationale: str | None = None,
        instruction: str | None = None,
    ) -> CheckpointResponse:
        """Deliver a human decision to the blocked run (called by the POST route).

        Args:
            run_id: The run whose gate is open.
            decision: The human's choice.
            rationale: Optional free-text rationale, logged verbatim.
            instruction: Required only for ``request_more_analysis``.

        Returns:
            The :class:`CheckpointResponse` handed to the waiting gate.

        Raises:
            CheckpointNotPendingError: If no open gate exists for ``run_id``.
        """
        future = self._pending.get(run_id)
        if future is None or future.done():
            raise CheckpointNotPendingError(
                f"no checkpoint is awaiting a decision for run {run_id!r}"
            )
        packet = self._packets.get(run_id)
        if decision is CheckpointDecision.APPROVE and (
            packet is None or not packet_allows_approval(packet)
        ):
            raise ValueError("approve requires a staged patch_id in the checkpoint")
        if decision is CheckpointDecision.ACKNOWLEDGE_INCOMPLETE and (
            packet is None or not packet_allows_incomplete_acknowledgement(packet)
        ):
            raise ValueError(
                "acknowledge_incomplete requires an incomplete or unverified audit"
            )
        response = CheckpointResponse(
            decision=decision,
            rationale=rationale or None,
            instruction=instruction or None,
            responded_at=datetime.now(tz=UTC),
        )
        future.set_result(response)
        self._bus.publish(
            {
                "kind": "checkpoint_resolved",
                "run_id": run_id,
                "decision": decision.value,
            }
        )
        return response
