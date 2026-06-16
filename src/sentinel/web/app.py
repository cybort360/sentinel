"""Starlette app factory for the SENTINEL web UI (architecture.md §18).

Read-only over the audit: the routes stream §10 trace events to the browser
(``GET /events``, Server-Sent Events), serve the raw ``SimulationMCP`` result
behind any cited ``trace_id`` (``GET /api/trace/{id}`` — the Golden Rule #1
drill-down), let a human drive the real blocking gate (``POST /checkpoint/{id}``
— Golden Rule #2), and kick off an audit run (``POST /api/run``). The app holds
no agent logic; everything domain-specific is injected by ``demo/web_demo.py``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from sentinel.observability.trace_logger import get_logger
from sentinel.orchestrator.checkpoint import CheckpointDecision
from sentinel.web.bus import TraceBus
from sentinel.web.responder import CheckpointNotPendingError, WebResponder

_log = get_logger("web")
_STATIC = Path(__file__).resolve().parent / "static"
_HEARTBEAT_SECONDS = 15.0
#: Largest contract source the upload endpoint accepts (bytes).
_MAX_UPLOAD_BYTES = 512 * 1024


class UploadRejectedError(Exception):
    """The uploaded contract is invalid (bad name/extension/size) — HTTP 400."""


class CompileError(Exception):
    """The uploaded contract did not compile — HTTP 422 (the solc/forge error)."""


#: Domain-specific run launcher: a coroutine that executes the audit session(s),
#: emitting §10 events as it goes. Receives the selected target contract path, or
#: ``None`` to run the full default showcase. Provided by the demo wiring.
RunLauncher = Callable[[str | None], Awaitable[None]]
#: Maps a ``trace_id`` to the recorded ``SimulationMCP`` result, or None.
TraceLookup = Callable[[str], Mapping[str, Any] | None]
#: Lists the selectable audit targets (each a mapping with at least ``path``).
TargetLister = Callable[[], list[Mapping[str, Any]]]
#: Handles a browser contract upload: given ``(filename, source_bytes)`` it
#: compiles the contract and registers it as a target, returning a mapping with
#: at least ``target`` (the new path) and ``contract``. Runs off the event loop
#: (``forge build`` is blocking); raises :class:`UploadRejectedError` /
#: :class:`CompileError` on bad input or a compile error.
UploadHandler = Callable[[str, bytes], Mapping[str, Any]]


class _RunState:
    """Tracks whether an audit run is in flight (single-run demo semantics)."""

    def __init__(self) -> None:
        self.running = False


def create_app(
    *,
    bus: TraceBus,
    responder: WebResponder,
    trace_lookup: TraceLookup,
    run_launcher: RunLauncher,
    target_lister: TargetLister | None = None,
    upload_handler: UploadHandler | None = None,
) -> Starlette:
    """Build the Starlette app wired to a bus, responder, and run launcher.

    Args:
        bus: The live event bus the SSE endpoint streams from.
        responder: The browser checkpoint responder the POST route resolves.
        trace_lookup: Resolves a ``trace_id`` to its real simulation result.
        run_launcher: Coroutine that runs the audit when ``POST /api/run`` fires;
            it receives the chosen target path (or ``None`` for the showcase).
        target_lister: Optional source of the selectable targets the browser
            picker offers (``GET /api/targets``). When omitted, the picker is
            empty and only the default showcase run is available.
        upload_handler: Optional handler that compiles a browser-uploaded
            contract and registers it as a target (``POST /api/upload``). When
            omitted, the upload route is not mounted.

    Returns:
        The configured :class:`starlette.applications.Starlette` app.
    """
    state = _RunState()
    list_targets: TargetLister = target_lister or (lambda: [])

    async def index(request: Request) -> Response:
        """Serve the single-page UI."""
        return FileResponse(_STATIC / "index.html")

    async def events(request: Request) -> Response:
        """Stream trace + control events to the browser as SSE."""
        return StreamingResponse(
            _event_stream(bus),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    async def get_trace(request: Request) -> Response:
        """Return the recorded SimulationMCP result behind a cited trace_id."""
        trace_id = request.path_params["trace_id"]
        result = trace_lookup(trace_id)
        if result is None:
            return JSONResponse(
                {"error": "unknown trace_id", "trace_id": trace_id}, 404
            )
        return JSONResponse({"trace_id": trace_id, **dict(result)})

    async def checkpoint(request: Request) -> Response:
        """Deliver a human decision to the blocked run (Golden Rule #2)."""
        run_id = request.path_params["run_id"]
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse({"error": "invalid JSON body"}, 400)
        try:
            decision = CheckpointDecision(str(body.get("decision", "")))
        except ValueError:
            return JSONResponse(
                {"error": "decision must be one of " + _DECISION_CHOICES}, 400
            )
        try:
            response = responder.resolve(
                run_id,
                decision,
                rationale=body.get("rationale"),
                instruction=body.get("instruction"),
            )
        except CheckpointNotPendingError as exc:
            return JSONResponse({"error": str(exc)}, 409)
        except ValueError as exc:  # e.g. request_more_analysis without instruction
            return JSONResponse({"error": str(exc)}, 400)
        return JSONResponse({"ok": True, "decision": response.decision.value})

    async def list_targets_route(request: Request) -> Response:
        """Return the selectable audit targets for the browser picker."""
        return JSONResponse({"targets": list(list_targets())})

    async def start_run(request: Request) -> Response:
        """Kick off an audit run (no-op if one is already in flight).

        An optional JSON body ``{"target": "<path>"}`` selects which contract to
        audit; the path is whitelisted against ``target_lister`` so the browser
        can only launch known sandbox targets (no arbitrary filesystem paths
        reach the orchestrator). An empty/absent body runs the full showcase.
        """
        if state.running:
            return JSONResponse({"error": "a run is already in progress"}, 409)
        target = await _selected_target(request)
        if target is not None:
            known = {str(t.get("path")) for t in list_targets()}
            if target not in known:
                return JSONResponse({"error": "unknown target", "target": target}, 400)
        state.running = True
        asyncio.create_task(_run(state, bus, run_launcher, target))
        return JSONResponse({"ok": True, "target": target})

    async def upload(request: Request) -> Response:
        """Compile a browser-uploaded contract and register it as a target.

        The raw ``.sol`` source is the request body; the filename is the
        ``name`` query param. ``forge build`` is blocking, so the handler runs
        off the event loop. The compiled contract becomes selectable via
        ``GET /api/targets`` and runnable via ``POST /api/run``.
        """
        if upload_handler is None:
            return JSONResponse({"error": "uploads are not enabled"}, 404)
        body = await request.body()
        if len(body) > _MAX_UPLOAD_BYTES:
            return JSONResponse({"error": "contract too large"}, 413)
        name = request.query_params.get("name", "")
        try:
            result = await asyncio.to_thread(upload_handler, name, body)
        except UploadRejectedError as exc:
            return JSONResponse({"error": str(exc)}, 400)
        except CompileError as exc:
            return JSONResponse({"error": str(exc)}, 422)
        except Exception as exc:  # noqa: BLE001 — never crash the server (Rule 4)
            _log.error("[DEGRADED] contract upload failed", error=str(exc))
            return JSONResponse({"error": "upload failed"}, 500)
        return JSONResponse({"ok": True, **dict(result)})

    async def healthz(request: Request) -> Response:
        """Liveness probe for the container healthcheck."""
        return JSONResponse({"ok": True, "subscribers": bus.subscriber_count})

    @asynccontextmanager
    async def _lifespan(app: Starlette) -> AsyncIterator[None]:
        """Bind the running loop so cross-thread publishes can be delivered."""
        bus.bind_loop(asyncio.get_running_loop())
        yield

    return Starlette(
        lifespan=_lifespan,
        routes=[
            Route("/", index),
            Route("/events", events),
            Route("/api/targets", list_targets_route),
            Route("/api/trace/{trace_id}", get_trace),
            Route("/checkpoint/{run_id}", checkpoint, methods=["POST"]),
            Route("/api/run", start_run, methods=["POST"]),
            Route("/api/upload", upload, methods=["POST"]),
            Route("/healthz", healthz),
        ],
    )


_DECISION_CHOICES = ", ".join(d.value for d in CheckpointDecision)


def _sse(event: Mapping[str, Any]) -> bytes:
    """Encode one event as an SSE ``data:`` frame (str-coercing odd values)."""
    return f"data: {json.dumps(event, default=str)}\n\n".encode()


async def _event_stream(bus: TraceBus) -> AsyncIterator[bytes]:
    """Yield SSE frames from the bus, with periodic heartbeats.

    No explicit disconnect poll: when the browser closes the stream Starlette
    cancels this generator, and the ``finally`` unsubscribes. Heartbeat comments
    keep idle connections (and proxies) alive between events.
    """
    subscription = bus.subscribe()
    try:
        while True:
            try:
                event = await asyncio.wait_for(
                    subscription.__anext__(), timeout=_HEARTBEAT_SECONDS
                )
            except TimeoutError:
                yield b": ping\n\n"
                continue
            yield _sse(event)
    finally:
        await subscription.aclose()


async def _selected_target(request: Request) -> str | None:
    """Extract the optional ``target`` from the run request body (or None)."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return None  # empty / non-JSON body -> default showcase
    if not isinstance(body, dict):
        return None
    target = body.get("target")
    return str(target) if target else None


async def _run(
    state: _RunState, bus: TraceBus, run_launcher: RunLauncher, target: str | None
) -> None:
    """Run the audit, publishing terminal control events; degrade on failure."""
    try:
        await run_launcher(target)
        bus.publish({"kind": "all_complete"})
    except Exception as exc:  # noqa: BLE001 — surface, never crash the server (Rule 4)
        _log.error("[DEGRADED] web audit run failed", error=str(exc))
        bus.publish({"kind": "run_error", "error": str(exc)})
    finally:
        state.running = False
