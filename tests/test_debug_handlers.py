"""Smoke tests for ``dragon_voice/handlers/debug.py``.

These cover the thin wiring only — the "handler returns a sensible
status when its dep isn't ready" paths.  The ``_iter_surface_sessions``
iteration path is exercised via a minimal stub.

Run:
    python3 -m pytest -v tests/test_debug_handlers.py
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import MagicMock

from aiohttp import web

from dragon_voice.handlers import debug as dbg


class _ReqWithJson:
    """Minimal async-json stub for the widget emitters.

    aiohttp ``web.Request.json()`` is an awaitable; we only need the
    parsed body + ``query`` dict, so build the smallest stand-in.
    """

    def __init__(self, body: dict | None = None, query: dict | None = None):
        self._body = body or {}
        self.query = query or {}

    async def json(self):
        return self._body


class DebugMemHandlerTests(unittest.TestCase):
    """``handle_debug_mem`` fails gracefully when tracemalloc is off."""

    def test_returns_503_when_tracemalloc_disabled(self):
        # Guard against the test accidentally importing with tracing
        # already on from a prior test in the same process.
        import tracemalloc
        if tracemalloc.is_tracing():
            tracemalloc.stop()

        req = MagicMock()
        req.query = {}
        server = MagicMock()

        async def go():
            resp = await dbg.handle_debug_mem(req, server=server)
            return resp

        resp: web.Response = asyncio.run(go())
        self.assertEqual(resp.status, 503)
        payload = json.loads(resp.body)
        self.assertEqual(payload["error"], "tracemalloc_disabled")


class WidgetHandlerTests(unittest.TestCase):
    """Each widget emitter returns 503 when surface_mgr is None and
    400 where it validates inputs."""

    def test_chart_returns_503_when_surface_mgr_missing(self):
        req = _ReqWithJson()

        async def go():
            return await dbg.handle_debug_widget_chart(req, surface_mgr=None)

        resp = asyncio.run(go())
        self.assertEqual(resp.status, 503)
        self.assertEqual(json.loads(resp.body)["error"], "surface_mgr not ready")

    def test_prompt_returns_503_when_surface_mgr_missing(self):
        req = _ReqWithJson()

        async def go():
            return await dbg.handle_debug_widget_prompt(req, surface_mgr=None)

        resp = asyncio.run(go())
        self.assertEqual(resp.status, 503)

    def test_card_returns_503_when_surface_mgr_missing(self):
        req = _ReqWithJson()

        async def go():
            return await dbg.handle_debug_widget_card(req, surface_mgr=None)

        resp = asyncio.run(go())
        self.assertEqual(resp.status, 503)

    def test_media_requires_url(self):
        # url is a required field — missing should 400 regardless of
        # surface_mgr state.
        req = _ReqWithJson(body={})

        async def go():
            return await dbg.handle_debug_widget_media(req, surface_mgr=None)

        resp = asyncio.run(go())
        self.assertEqual(resp.status, 400)
        self.assertEqual(json.loads(resp.body)["error"], "url required")

    def test_media_returns_503_when_surface_mgr_missing_but_url_present(self):
        req = _ReqWithJson(body={"url": "http://example.com/a.jpg"})

        async def go():
            return await dbg.handle_debug_widget_media(req, surface_mgr=None)

        resp = asyncio.run(go())
        self.assertEqual(resp.status, 503)


class WidgetChartEmitTests(unittest.TestCase):
    """End-to-end smoke: chart emitter iterates the surface manager's
    sessions and calls ``surface.chart`` once per session.

    Uses a stub SurfaceManager that exposes ``_sessions`` the same way
    the real one does so ``_iter_surface_sessions`` can walk it.
    """

    def test_emits_one_chart_per_session(self):
        # Two fake sessions, each with a mock surface.
        calls: list[tuple[str, str]] = []

        class _Surface:
            def __init__(self, name: str):
                self.name = name

            async def chart(self, *, title, values, chart_max, skill_id, card_id):
                calls.append((self.name, card_id))

        class _State:
            def __init__(self, name: str):
                self.surface = _Surface(name)

        class _SurfaceMgr:
            def __init__(self):
                self._sessions = {
                    "session_aaaaaaaa": _State("a"),
                    "session_bbbbbbbb": _State("b"),
                }

        req = _ReqWithJson(body={"title": "t", "values": [1, 2, 3], "chart_max": 10})
        mgr = _SurfaceMgr()

        async def go():
            return await dbg.handle_debug_widget_chart(req, surface_mgr=mgr)

        resp = asyncio.run(go())
        self.assertEqual(resp.status, 200)
        payload = json.loads(resp.body)
        self.assertEqual(payload["emitted"], 2)
        self.assertEqual(payload["values"], [1, 2, 3])
        # Each surface saw exactly one chart call, with a card_id that
        # encodes the first 6 chars of its session id.
        self.assertEqual(len(calls), 2)
        self.assertIn(("a", "audit_chart_sessio"), calls)
        self.assertIn(("b", "audit_chart_sessio"), calls)


if __name__ == "__main__":
    unittest.main()
