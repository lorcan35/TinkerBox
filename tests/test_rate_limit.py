"""Wave 15 W15-H01 + W15-H06 regression — rate-limit middleware.

State-changing endpoints (DELETE /devices, POST /sessions/*/end,
upload, etc.) and SSE reconnects (/chat) get capped per-IP so a broken
client or malicious loop can't hammer Dragon.
"""

from __future__ import annotations

import asyncio
import os

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from dragon_voice.config import load_config
from dragon_voice import server as srv_mod


class _StubServer(srv_mod.VoiceServer):
    """Minimal subclass skipping backend init — we only need the
    middleware + the server's rate-bucket state."""
    def __init__(self, config):
        self._config = config
        self._rate_buckets = {}


def _build_app(stub):
    async def _ok(req):
        return web.json_response({"ok": True})

    @web.middleware
    async def _rl_mw(request, handler):
        return await stub._rate_limit_middleware(request, handler)

    app = web.Application(middlewares=[_rl_mw])
    # Handlers that match rate-limit rules:
    app.router.add_delete("/api/v1/devices/{id}", _ok)
    app.router.add_post("/api/v1/sessions/{id}/end", _ok)
    app.router.add_post("/api/media/upload", _ok)
    # An un-rate-limited reader for comparison.
    app.router.add_get("/api/v1/sessions", _ok)
    return app


def _stub():
    os.environ.setdefault("DRAGON_API_TOKEN", "rl-probe")
    os.environ.setdefault("TINKERCLAW_TOKEN", "rl-probe")
    return _StubServer(load_config())


def _run(coro):
    return asyncio.run(coro)


def test_delete_device_rate_limit_triggers_429():
    stub = _stub()
    app = _build_app(stub)

    async def go():
        async with TestServer(app) as s, TestClient(s) as c:
            # DELETE /devices cap = 20 / 60 s
            first_ok = 0
            rate_limited = 0
            for _ in range(25):
                r = await c.delete("/api/v1/devices/abc")
                if r.status == 200:
                    first_ok += 1
                elif r.status == 429:
                    rate_limited += 1
                    body = await r.json()
                    assert body["error"] == "rate_limited"
                    assert r.headers["Retry-After"].isdigit()
            # First 20 should succeed, the rest 429.
            assert first_ok == 20
            assert rate_limited == 5
    _run(go())


def test_read_only_endpoint_unthrottled():
    """Read-only paths don't match any rule and should never 429."""
    stub = _stub()
    app = _build_app(stub)

    async def go():
        async with TestServer(app) as s, TestClient(s) as c:
            for _ in range(30):
                r = await c.get("/api/v1/sessions")
                assert r.status == 200
    _run(go())


def test_upload_rate_limit_covers_post():
    """Upload path has its own rule (30 / 60 s)."""
    stub = _stub()
    app = _build_app(stub)

    async def go():
        async with TestServer(app) as s, TestClient(s) as c:
            ok = 0
            for _ in range(35):
                r = await c.post("/api/media/upload", data=b"x" * 32)
                if r.status == 200:
                    ok += 1
                elif r.status == 429:
                    pass
            # Multiple rules may match this path (DELETE /sessions/
            # prefix doesn't match POST /api/media/upload, so only the
            # upload rule applies: cap=30).
            assert ok == 30
    _run(go())


def test_different_paths_have_independent_buckets():
    """DELETE /devices/foo and DELETE /devices/bar share a bucket only
    if they hash to the same key.  The current impl keys on *path*, so
    different IDs ARE different buckets.  That's by design — a client
    that legitimately hits 20 device deletes shouldn't be penalised if
    they all target different devices."""
    stub = _stub()
    app = _build_app(stub)

    async def go():
        async with TestServer(app) as s, TestClient(s) as c:
            # Fill one bucket.
            for _ in range(20):
                r = await c.delete("/api/v1/devices/foo")
                assert r.status == 200
            # 21st on foo → 429
            r = await c.delete("/api/v1/devices/foo")
            assert r.status == 429
            # But bar is a fresh bucket.
            r = await c.delete("/api/v1/devices/bar")
            assert r.status == 200
    _run(go())
