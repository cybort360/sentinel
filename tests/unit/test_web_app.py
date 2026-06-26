"""ASGI smoke test for the web app (architecture.md §18).

Drives the Starlette app in-process via httpx's ASGI transport (no network, no
Anvil): health, the Rule-1 evidence lookup, checkpoint error handling, and a full
SSE run where a published trace event and the terminal ``all_complete`` reach a
streaming subscriber.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import sentinel.web.app as web_app
from sentinel.orchestrator.checkpoint import DecisionPacket
from sentinel.orchestrator.schema import AgentRole, Outcome, Proposal, RiskProfile
from sentinel.web import (
    CompileError,
    TraceBus,
    UploadRejectedError,
    WebResponder,
    create_app,
)
from sentinel.web.app import _event_stream


def test_web_app_smoke() -> None:
    asyncio.run(_smoke())


def test_checkpoint_approve_button_state_and_label() -> None:
    html = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "sentinel"
        / "web"
        / "static"
        / "index.html"
    ).read_text()

    assert "rp.dynamic_verification_status" in html
    assert "rp.outcome !== 'tool_failure'" in html
    assert (
        "const incompleteAudit = rp.outcome === 'tool_failure' || !hasProposalPatch"
        in html
    )
    assert "btnAcknowledge" in html
    assert "dbtn primary" in html
    assert "Acknowledge incomplete audit" in html
    assert "resolveCheckpoint('acknowledge_incomplete')" in html
    assert "Audit incomplete - patch staging failed" in html
    assert 'placeholder="Optional rationale"' in html
    assert "Approve &amp; deploy" in html
    assert "Approve patch with residual risk" in html
    assert "unverified" in html


def test_run_complete_finishes_ui_without_refresh() -> None:
    html = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "sentinel"
        / "web"
        / "static"
        / "index.html"
    ).read_text()

    assert "function finishRun" in html
    assert "case 'run_complete': renderRunComplete(f); break;" in html
    assert "finishRun(f, {terminal}" in html
    assert "State.isRunning=false" in html
    assert "setRunButtonIdle('Run again')" in html
    assert "runSummaryCard" in html
    assert "resolveTerminalPayoffFallbacks" in html
    assert "renderEfficiencySkipped" in html
    assert "renderCrossSessionSkipped" in html
    assert "Benchmark skipped for uploaded target." in html
    assert "Not applicable for a single uploaded run." in html
    assert "Static audit completed, but dynamic verification was unavailable." in html
    assert "Patch staging failed, no staged source exists." in html
    assert "patch staging failed / tool failure" in html
    assert "return 'Unverified'" in html
    assert "return f.final_proposal || 'unavailable'" in html
    assert "return 'Unverified'" in html
    assert (
        "const unverified = String(outcome||'') === 'dynamic_verification_unavailable';"
    ) in html
    assert "dom.tRisk.textContent = unverified ? 'Unverified' : fmtPct(risk);" in html


def test_sse_stream_emits_events_normally() -> None:
    asyncio.run(_sse_stream_emits_events_normally())


def test_sse_stream_heartbeat_survives_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_sse_stream_heartbeat_survives_timeout(monkeypatch))


def test_sse_client_disconnect_cleans_subscription() -> None:
    asyncio.run(_sse_client_disconnect_cleans_subscription())


def test_sse_closed_subscription_exits_without_runtime_error() -> None:
    asyncio.run(_sse_closed_subscription_exits_without_runtime_error())


def test_repeated_sse_refreshes_cleanup_subscriptions() -> None:
    asyncio.run(_repeated_sse_refreshes_cleanup_subscriptions())


async def _smoke() -> None:
    bus = TraceBus()
    responder = WebResponder(bus)
    launched: list[str | None] = []

    async def launcher(target: str | None) -> None:
        launched.append(target)
        bus.publish(
            {
                "kind": "trace",
                "summary": "run start",
                "trace_id": "t1",
                "demo_tag": "SYSTEM DECISION",
            }
        )

    def lookup(trace_id: str) -> dict[str, object] | None:
        return {"tool": "measure_gas", "gas_used": 21000} if trace_id == "t1" else None

    def targets() -> list[dict[str, object]]:
        return [{"path": "sandbox/contracts/Vault.sol", "label": "Vault"}]

    def upload(name: str, source: bytes) -> dict[str, object]:
        if not name.endswith(".sol"):
            raise UploadRejectedError("must be .sol")
        if b"syntax error" in source:
            raise CompileError("ParserError: boom")
        return {"target": f"sandbox/contracts/{name}", "contract": name[:-4]}

    app = create_app(
        bus=bus,
        responder=responder,
        trace_lookup=lookup,
        run_launcher=launcher,
        target_lister=targets,
        upload_handler=upload,
    )
    bus.bind_loop(asyncio.get_running_loop())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/healthz")).status_code == 200

        ok = await client.get("/api/trace/t1")
        assert ok.status_code == 200 and ok.json()["gas_used"] == 21000
        assert (await client.get("/api/trace/missing")).status_code == 404

        # The picker source is exposed for the browser.
        tgt = await client.get("/api/targets")
        assert tgt.status_code == 200
        assert tgt.json()["targets"][0]["path"] == "sandbox/contracts/Vault.sol"

        # An unknown target is rejected (only whitelisted paths reach the graph).
        bad = await client.post("/api/run", json={"target": "/etc/passwd"})
        assert bad.status_code == 400

        # Upload: a good contract compiles (200); bad name -> 400; bad source -> 422.
        good = await client.post(
            "/api/upload?name=Vault.sol", content=b"contract Vault {}"
        )
        assert good.status_code == 200 and good.json()["contract"] == "Vault"
        assert (
            await client.post("/api/upload?name=Vault.txt", content=b"x")
        ).status_code == 400
        assert (
            await client.post("/api/upload?name=Bad.sol", content=b"syntax error")
        ).status_code == 422

        # No gate is open -> 409; an unknown decision -> 400.
        assert (
            await client.post("/checkpoint/none", json={"decision": "approve"})
        ).status_code == 409
        bogus = await client.post("/checkpoint/none", json={"decision": "bogus"})
        assert bogus.status_code == 400
        assert "acknowledge_incomplete" in bogus.json()["error"]

        packet = _tool_failure_packet("run-ack")
        gate_task = asyncio.create_task(responder.ask(packet))
        for _ in range(50):
            if responder.is_pending("run-ack"):
                break
            await asyncio.sleep(0.005)
        ack = await client.post(
            "/checkpoint/run-ack",
            json={"decision": "acknowledge_incomplete"},
        )
        assert ack.status_code == 200
        assert ack.json()["decision"] == "acknowledge_incomplete"
        response = await asyncio.wait_for(gate_task, timeout=1.0)
        assert response.decision.value == "acknowledge_incomplete"

        packet_alias = _tool_failure_packet("run-ack-alias")
        alias_task = asyncio.create_task(responder.ask(packet_alias))
        for _ in range(50):
            if responder.is_pending("run-ack-alias"):
                break
            await asyncio.sleep(0.005)
        alias = await client.post(
            "/checkpoint/run-ack-alias",
            json={"action": "acknowledge_incomplete_audit"},
        )
        assert alias.status_code == 200
        assert alias.json()["decision"] == "acknowledge_incomplete"
        await asyncio.wait_for(alias_task, timeout=1.0)

        normal_packet = _normal_approval_packet("run-normal")
        normal_task = asyncio.create_task(responder.ask(normal_packet))
        for _ in range(50):
            if responder.is_pending("run-normal"):
                break
            await asyncio.sleep(0.005)
        bad_ack = await client.post(
            "/checkpoint/run-normal",
            json={"decision": "acknowledge_incomplete"},
        )
        assert bad_ack.status_code == 400
        assert "incomplete or unverified audit" in bad_ack.json()["error"]
        reject = await client.post(
            "/checkpoint/run-normal",
            json={"decision": "reject"},
        )
        assert reject.status_code == 200
        await asyncio.wait_for(normal_task, timeout=1.0)

        # An empty body runs the default showcase (launcher gets target=None).
        assert (await client.post("/api/run")).status_code == 200
        await asyncio.sleep(0.15)  # let the run task publish trace + all_complete
        assert launched == [None]

    # httpx's ASGI test transport BUFFERS streaming responses (it waits for the
    # body to finish — ours is an infinite SSE stream), so the live /events route
    # can't be read through it. Read the same SSE frames through the actual
    # encoder + bus instead; uvicorn streams these fine in production.
    got = await _drain_sse(bus)
    kinds = {e.get("kind") for e in got}
    assert "trace" in kinds, got
    assert "all_complete" in kinds, got


def _tool_failure_packet(run_id: str) -> DecisionPacket:
    return DecisionPacket(
        run_id=run_id,
        target="sandbox/contracts/audit_workdir/Vulnerable.sol",
        gated_reasons=["Audit incomplete because a tool failed (§4 Rule 4)"],
        proposal=None,
        assessment=None,
        review=None,
        risk_profile=RiskProfile(
            run_id=run_id,
            outcome=Outcome.TOOL_FAILURE,
            final_proposal=None,
            residual_risk_pct=0.0,
            residual_risk_description=(
                "Patch staging failed, no staged source exists."
            ),
            iterations=1,
            tokens_total=0,
            trace_ids=["sim-1"],
        ),
        memory_records_used=[],
        topic_tags=["uploaded-contract"],
    )


def _normal_approval_packet(run_id: str) -> DecisionPacket:
    return DecisionPacket(
        run_id=run_id,
        target="sandbox/contracts/Vault.sol",
        gated_reasons=["Applying a patch to the protocol (§7.1.2)"],
        proposal=Proposal(
            proposal_id="p1",
            run_id=run_id,
            iteration=1,
            patch_id="patch-1",
            proposed_by=AgentRole.ARBITRATOR,
            summary="stage normal patch",
            trace_ids=["sim-1"],
        ),
        assessment=None,
        review=None,
        risk_profile=RiskProfile(
            run_id=run_id,
            outcome=Outcome.CONSENSUS,
            final_proposal="patch-1",
            residual_risk_pct=0.0,
            residual_risk_description="patch verified",
            iterations=1,
            tokens_total=0,
            trace_ids=["sim-1"],
        ),
        memory_records_used=[],
        topic_tags=["vault"],
    )


async def _sse_stream_emits_events_normally() -> None:
    bus = TraceBus()
    bus.bind_loop(asyncio.get_running_loop())
    bus.publish({"kind": "trace", "summary": "hello"})
    await asyncio.sleep(0)

    stream = _event_stream(bus)
    try:
        frame = await asyncio.wait_for(stream.__anext__(), timeout=1.0)
    finally:
        await stream.aclose()

    event = json.loads(frame.decode().removeprefix("data:").strip())
    assert event["kind"] == "trace"
    assert event["summary"] == "hello"


async def _sse_stream_heartbeat_survives_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class IdleBus:
        async def subscribe(self):
            await asyncio.Event().wait()
            if False:
                yield {}

    monkeypatch.setattr(web_app, "_HEARTBEAT_SECONDS", 0.01)
    stream = _event_stream(IdleBus())  # type: ignore[arg-type]
    try:
        frame = await asyncio.wait_for(stream.__anext__(), timeout=1.0)
    finally:
        await stream.aclose()

    assert frame == b": ping\n\n"


async def _sse_client_disconnect_cleans_subscription() -> None:
    class DisconnectAfterFirstFrame:
        def __init__(self) -> None:
            self.calls = 0

        async def is_disconnected(self) -> bool:
            self.calls += 1
            return self.calls > 1

    bus = TraceBus()
    bus.bind_loop(asyncio.get_running_loop())
    bus.publish({"kind": "trace", "summary": "first"})
    await asyncio.sleep(0)
    request = DisconnectAfterFirstFrame()

    stream = _event_stream(bus, request)  # type: ignore[arg-type]
    frame = await asyncio.wait_for(stream.__anext__(), timeout=1.0)
    assert frame.startswith(b"data:")
    assert bus.subscriber_count == 1
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(stream.__anext__(), timeout=1.0)
    assert bus.subscriber_count == 0


async def _sse_closed_subscription_exits_without_runtime_error() -> None:
    class ClosedBus:
        async def subscribe(self):
            if False:
                yield {}

    stream = _event_stream(ClosedBus())  # type: ignore[arg-type]
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(stream.__anext__(), timeout=1.0)


async def _repeated_sse_refreshes_cleanup_subscriptions() -> None:
    bus = TraceBus()
    bus.bind_loop(asyncio.get_running_loop())

    for i in range(5):
        bus.publish({"kind": "trace", "summary": f"refresh-{i}"})
        await asyncio.sleep(0)
        stream = _event_stream(bus)
        frame = await asyncio.wait_for(stream.__anext__(), timeout=1.0)
        assert frame.startswith(b"data:")
        await stream.aclose()
        assert bus.subscriber_count == 0


async def _drain_sse(bus: TraceBus) -> list[dict[str, object]]:
    """Read SSE ``data:`` frames from the live stream until ``all_complete``."""
    events: list[dict[str, object]] = []
    stream = _event_stream(bus)
    try:
        while True:
            frame = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
            if not frame.startswith(b"data:"):
                continue  # heartbeat
            event = json.loads(frame.decode().removeprefix("data:").strip())
            events.append(event)
            if event.get("kind") == "all_complete":
                return events
    finally:
        await stream.aclose()
