"""Web UI for SENTINEL — a read-only renderer of the §10 trace stream (§18).

It also drives the browser-based Human Checkpoint (architecture.md §18).
This package is deliberately generic: it owns the live event bus
(:class:`~sentinel.web.bus.TraceBus`), the browser checkpoint responder
(:class:`~sentinel.web.responder.WebResponder`), and the Starlette app factory
(:func:`~sentinel.web.app.create_app`), but knows nothing about which contracts
or agents run. The SENTINEL-specific wiring lives in ``demo/web_demo.py`` so the
dependency only ever points demo -> src, never the reverse.

The UI never originates an agent claim — it only displays events the orchestrator
already emitted (Golden Rule #1) and drives the real blocking gate (Golden
Rule #2).
"""

from __future__ import annotations

from sentinel.web.app import create_app
from sentinel.web.bus import TraceBus
from sentinel.web.responder import WebResponder

__all__ = ["TraceBus", "WebResponder", "create_app"]
