"""ASGI smoke test for the web app (architecture.md §18).

Drives the Starlette app in-process via httpx's ASGI transport (no network, no
Anvil): health, the Rule-1 evidence lookup, checkpoint error handling, and a full
SSE run where a published trace event and the terminal ``all_complete`` reach a
streaming subscriber.
"""

from __future__ import annotations

import asyncio
import json

from httpx import ASGITransport, AsyncClient

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
        assert (
            await client.post("/checkpoint/none", json={"decision": "bogus"})
        ).status_code == 400

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
