"""Golden-trace integration test for the §12 demo (CLAUDE.md §6).

Runs the *entire* demo end-to-end against a REAL local Anvil sandbox (no mocked
MCP — that is the whole point per CLAUDE.md §6 / Rule 3) and snapshots the
**schema** of the structured log stream, not its exact wording. Per
architecture.md §10 the demo trace is non-deterministic in phrasing (and the
agents emit fresh ``trace_id``\\s every run), so asserting on wording would be
brittle and wrong; instead we assert the load-bearing invariants:

* all §10 demo tags are emitted from the same structured stream;
* every claim-bearing demo event (``ADVERSARY VETO``, ``ARBITRATION``,
  ``ANNOTATED RESIDUAL RISK``) carries a non-null ``trace_id`` (Golden Rule #1);
* every demo-tagged event conforms to the :class:`TraceEvent` §10 schema and
  renders to a bracketed ``[TAG] …`` line from that same event;
* cross-session memory genuinely discriminates (§6.4): the finding-driven
  recall surfaces the batching record for Contract 1 and the access-control
  record for Contract 2 — *different* records; and
* both sessions reach a real ``constraints_unsatisfied`` outcome.

If the orchestration graph regresses (a tag stops firing, a claim loses its
trace, the two sessions collapse onto one memory record), one of these turns
red. Run with: ``make check-integration`` (requires Foundry installed).
"""

from __future__ import annotations

import asyncio
import io
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from demo.run_demo import AutoApproveResponder, DemoReport, run_demo
from rich.console import Console
from structlog.testing import capture_logs
from tests.integration.sandbox_build import build_demo_contracts

from sentinel.mcp_servers.codebase_mcp.config import CodebaseConfig
from sentinel.mcp_servers.codebase_mcp.engine import CodebaseEngine
from sentinel.mcp_servers.simulation_mcp.anvil import AnvilProcess
from sentinel.mcp_servers.simulation_mcp.config import SimulationConfig
from sentinel.mcp_servers.simulation_mcp.engine import SimulationEngine
from sentinel.memory.seed import seed_store
from sentinel.memory.store import MemoryStore
from sentinel.observability.trace_logger import DemoTag, TraceEvent, render_trace_line

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SANDBOX = _REPO_ROOT / "sandbox"
# Dedicated port so we never collide with build-sandbox (8545) or the
# SimulationMCP integration node (8546).
_TEST_PORT = 8547

# Seed record ids the §6.4 cross-session demonstration is expected to surface.
_BATCHING_ID = "00000000-0000-0000-0000-0000000000a1"
_ACCESS_CONTROL_ID = "00000000-0000-0000-0000-0000000000a2"

# The claim-bearing demo tags: anything asserting a simulation fact must cite a
# trace (Golden Rule #1). SYSTEM DECISION / CONSTRAINTS UNSATISFIED / HUMAN
# CHECKPOINT are control-flow events and may legitimately have no trace_id.
_CLAIM_TAGS = {
    DemoTag.ADVERSARY_VETO.value,
    DemoTag.ARBITRATION.value,
    DemoTag.ANNOTATED_RESIDUAL_RISK.value,
}


@pytest.fixture(scope="module")
def demo_run() -> Iterator[tuple[list[dict[str, object]], DemoReport]]:
    """Boot a real sandbox, run the whole demo once, capture (logs, report)."""
    if shutil.which("anvil") is None or shutil.which("forge") is None:
        pytest.skip("Foundry (anvil/forge) not installed")
    build_demo_contracts(_SANDBOX)

    anvil = AnvilProcess(host="127.0.0.1", port=_TEST_PORT)
    anvil.start()
    memory = MemoryStore(":memory:")
    try:
        seed_store(memory)
        simulation = SimulationEngine(
            SimulationConfig(
                host="127.0.0.1", port=_TEST_PORT, artifacts_dir=_SANDBOX / "out"
            )
        )
        codebase = CodebaseEngine(CodebaseConfig(repo_root=_REPO_ROOT))
        # Discard the rich rendering; the test inspects the structured stream.
        console = Console(file=io.StringIO())
        with capture_logs() as logs:
            report = asyncio.run(
                run_demo(
                    simulation=simulation,
                    codebase=codebase,
                    memory=memory,
                    responder=AutoApproveResponder(),
                    console=console,
                )
            )
        yield logs, report
    finally:
        memory.close()
        anvil.stop()


def _demo_events(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    """The structured events that carry a §10 demo tag."""
    return [e for e in logs if e.get("demo_tag")]


def test_all_demo_tags_are_emitted(
    demo_run: tuple[list[dict[str, object]], DemoReport],
) -> None:
    logs, _ = demo_run
    seen = {e["demo_tag"] for e in _demo_events(logs)}
    assert seen == {tag.value for tag in DemoTag}


def test_claim_events_cite_a_real_trace_id(
    demo_run: tuple[list[dict[str, object]], DemoReport],
) -> None:
    # Golden Rule #1: every veto / residual-risk claim traces to a SimulationMCP
    # call. A real run emits fresh ids, so we assert presence + non-emptiness,
    # never a fixed value.
    claims = [e for e in _demo_events(demo_run[0]) if e["demo_tag"] in _CLAIM_TAGS]
    assert claims, "expected at least one veto/residual-risk claim event"
    for event in claims:
        trace_id = event.get("trace_id")
        assert isinstance(trace_id, str) and trace_id.strip(), event


def test_every_demo_event_matches_the_schema_and_renders(
    demo_run: tuple[list[dict[str, object]], DemoReport],
) -> None:
    # Schema, not wording: each event validates under the §10 TraceEvent and
    # renders to a bracketed line from that same structured event (no separate
    # narration path). capture_logs is pre-processor, so we normalise the two
    # fields structlog's processors would add (event->summary, ts).
    for event in _demo_events(demo_run[0]):
        normalised = {**event, "summary": event.get("event", ""), "ts": "n/a"}
        normalised.pop("event", None)
        model = TraceEvent.model_validate(normalised)
        assert model.demo_tag in {tag.value for tag in DemoTag}
        line = render_trace_line(normalised)
        assert line.startswith(f"[{model.demo_tag}]")


def test_cross_session_memory_discriminates(
    demo_run: tuple[list[dict[str, object]], DemoReport],
) -> None:
    # §6.4: the finding-driven recall surfaces a *different* prior incident per
    # contract — batching/latency for the billing contract, access-control for
    # the vault — proving retrieval generalises rather than being wired to one
    # path.
    _, report = demo_run
    billing, vault = report.sessions
    assert billing.top_memory == _BATCHING_ID
    assert vault.top_memory == _ACCESS_CONTROL_ID
    assert billing.top_memory != vault.top_memory


def test_both_sessions_reach_constraints_unsatisfied(
    demo_run: tuple[list[dict[str, object]], DemoReport],
) -> None:
    _, report = demo_run
    assert [s.outcome for s in report.sessions] == [
        "constraints_unsatisfied",
        "constraints_unsatisfied",
    ]
    # And each session's RiskProfile cited real traces (Rule 1, end to end).
    for session in report.sessions:
        assert session.trace_ids and all(t.strip() for t in session.trace_ids)


def test_efficiency_table_is_populated_from_real_runs(
    demo_run: tuple[list[dict[str, object]], DemoReport],
) -> None:
    # The §11 numbers must come from live SimulationMCP calls, not constants.
    _, report = demo_run
    eff = report.efficiency
    assert eff["trace_ids"] and all(str(t).strip() for t in eff["trace_ids"])
    assert isinstance(eff["naive_gas"], int) and eff["naive_gas"] > 0
    assert isinstance(eff["guarded_gas"], int) and eff["guarded_gas"] > 0
    # The reentrancy guard is a real, non-trivial code change -> measurable gas.
    assert eff["guarded_gas"] != eff["naive_gas"]
