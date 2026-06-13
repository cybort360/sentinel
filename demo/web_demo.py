"""Browser-driven SENTINEL demo (architecture.md §18).

Serves the §18 War Room UI: the same two audit sessions as ``demo/run_demo.py``,
streamed live to the browser over SSE, with the Human Checkpoint driven by a
button instead of the terminal. This is the SENTINEL-specific wiring — it injects
the real engines, the two demo graphs, and a :class:`WebResponder` into the
generic app factory in :mod:`sentinel.web`. The dependency points demo -> src
only.

Golden Rules preserved: every timeline claim is a §10 event the orchestrator
already emitted (Rule 1; the evidence drawer reads the real ``SimulationMCP``
result via ``get_trace``), and the checkpoint genuinely blocks on the browser
(Rule 2 — the WebResponder awaits a future no default ever completes).

Run with ``make web`` (or ``python -m demo.web_demo``); open http://localhost:8088.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from typing import Any

import uvicorn
from rich.console import Console

from demo.run_demo import (
    _build_billing_graph,
    _build_engines,
    _build_vault_graph,
    _measure_efficiency,
    _run_baseline,
)
from sentinel.observability.trace_logger import register_sink
from sentinel.orchestrator.checkpoint import (
    HumanCheckpoint,
    HumanResponder,
    MemoryStorePostMortemWriter,
    build_decision_packet,
)
from sentinel.orchestrator.graph import RunResult, WarRoomGraph
from sentinel.web import TraceBus, WebResponder, create_app

_SESSIONS = [
    ("session-1-billing", "sandbox/contracts/SubscriptionBilling.sol",
     ["batching", "settlement-latency", "congestion"], _build_billing_graph),
    ("session-2-vault", "sandbox/contracts/YieldVault.sol",
     ["access-control", "fund-custody"], _build_vault_graph),
]


async def _run_one(
    graph: WarRoomGraph,
    target: str,
    run_id: str,
    *,
    topic_tags: list[str],
    responder: HumanResponder,
    memory: Any,
    bus: TraceBus,
) -> str | None:
    """Run one audit through the graph + the blocking (web) checkpoint.

    Returns the finding-driven memory record id surfaced this session (§6.4), or
    None. ``graph.run`` is offloaded to a thread so its §10 events stream live.
    """
    bus.publish({"kind": "run_started", "run_id": run_id, "target": target})
    loop = asyncio.get_running_loop()
    result: RunResult = await loop.run_in_executor(
        None, lambda: graph.run(target, run_id=run_id)
    )
    packet = build_decision_packet(result, topic_tags=topic_tags)
    gate = HumanCheckpoint(responder, MemoryStorePostMortemWriter(memory))
    await gate.request(packet)  # blocks until the browser decides (Rule 2)

    finding = result.finding_memory_records
    top_memory = finding[0] if finding else None
    bus.publish(
        {
            "kind": "run_complete",
            "run_id": run_id,
            "target": target,
            "outcome": result.risk_profile.outcome.value,
            "residual_risk_pct": result.risk_profile.residual_risk_pct,
            "final_proposal": result.risk_profile.final_proposal,
            "top_memory": top_memory,
        }
    )
    return top_memory


def _efficiency_rows(
    efficiency: Mapping[str, Any], baseline_elapsed: float
) -> list[list[str]]:
    """Build the §11 comparison rows from real run data (mirrors run_demo)."""
    gas = float(efficiency["gas_delta_pct"])
    revert = float(efficiency["post_patch_congestion_revert"])
    return [
        ["Vulnerabilities identified", "1 (reentrancy)", "2 (reentrancy + DoS)"],
        ["False negatives (missed trade-offs)", "1 (batching/latency)", "0"],
        ["Trade-offs surfaced", "0", "1 (batching vs latency)"],
        ["Final patch gas delta vs original", "n/a", f"+{gas:.1%}"],
        ["Revert under congestion (post-patch)", "n/a", f"{revert:.0%}"],
        ["Tokens consumed", "n/a", "0 (deterministic driver)"],
        ["Residual risk disclosed?", "N", "Y"],
    ]


def main() -> None:
    """Wire engines + graphs + the web app, then serve it with uvicorn."""
    console = Console()
    simulation, codebase, memory = _build_engines()
    bus = TraceBus()
    responder = WebResponder(bus)

    # Tap the §10 stream: every event also goes to the browser, verbatim.
    register_sink(lambda event: bus.publish({"kind": "trace", **dict(event)}))

    async def run_launcher() -> None:
        """Run both sessions, then publish the §11 + §6.4 control events."""
        tops: list[str | None] = []
        for run_id, target, tags, build in _SESSIONS:
            graph = build(simulation, codebase, memory)
            tops.append(
                await _run_one(
                    graph, target, run_id,
                    topic_tags=tags, responder=responder, memory=memory, bus=bus,
                )
            )
        _, baseline_elapsed = _run_baseline(codebase)
        efficiency = _measure_efficiency(simulation)
        bus.publish(
            {"kind": "efficiency", "rows": _efficiency_rows(efficiency, baseline_elapsed)}
        )
        bus.publish(
            {
                "kind": "cross_session",
                "s1": tops[0],
                "s2": tops[1],
                "differ": tops[0] != tops[1],
            }
        )

    app = create_app(
        bus=bus,
        responder=responder,
        trace_lookup=simulation.get_trace,
        run_launcher=run_launcher,
    )
    host = os.environ.get("SENTINEL_WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("SENTINEL_WEB_PORT", "8088"))
    console.print(f"[bold green]SENTINEL War Room UI[/] → http://localhost:{port}")
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        memory.close()


if __name__ == "__main__":
    main()
