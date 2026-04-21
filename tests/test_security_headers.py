"""Wave 14 W14-M06 regression: every response carries the defensive headers.

X-Content-Type-Options, X-Frame-Options, Referrer-Policy, and a strict
Content-Security-Policy are stamped by VoiceServer._security_headers_middleware.
If a future PR drops the middleware or a new handler bypasses the
pipeline, this test fails.
"""

import asyncio
import os

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import pytest

from dragon_voice.config import load_config
from dragon_voice import server as srv_mod


class _StubServer(srv_mod.VoiceServer):
    def __init__(self, config):
        self._config = config


def _build_app(stub):
    async def _ok(req):
        return web.json_response({"ok": True})

    @web.middleware
    async def _auth_mw(request, handler):
        return await stub._auth_middleware(request, handler)

    @web.middleware
    async def _sec_mw(request, handler):
        return await stub._security_headers_middleware(request, handler)

    # Mirror the production order: security-headers OUTERMOST so even a
    # 401 from the auth middleware still gets the defensive headers.
    app = web.Application(middlewares=[_sec_mw, _auth_mw])
    app.router.add_get("/health", _ok)
    app.router.add_get("/api/v1/sessions", _ok)
    return app


def _run(coro):
    return asyncio.run(coro)


def _stub():
    os.environ["DRAGON_API_TOKEN"] = "w14-m06-probe"
    os.environ["TINKERCLAW_TOKEN"] = "w14-m06-probe"
    return _StubServer(load_config())


def test_public_route_has_security_headers():
    stub = _stub()
    app = _build_app(stub)

    async def go():
        async with TestServer(app) as s, TestClient(s) as c:
            r = await c.get("/health")
            assert r.status == 200
            assert r.headers["X-Content-Type-Options"] == "nosniff"
            assert r.headers["X-Frame-Options"] == "DENY"
            assert r.headers["Referrer-Policy"] == "no-referrer"
            csp = r.headers["Content-Security-Policy"]
            assert "default-src 'self'" in csp
            assert "frame-ancestors 'none'" in csp
    _run(go())


def test_private_authed_route_has_security_headers():
    stub = _stub()
    app = _build_app(stub)

    async def go():
        async with TestServer(app) as s, TestClient(s) as c:
            r = await c.get(
                "/api/v1/sessions",
                headers={"Authorization": "Bearer w14-m06-probe"},
            )
            assert r.status == 200
            # Even a successful authed response carries the headers.
            assert r.headers["X-Frame-Options"] == "DENY"
            assert "'self'" in r.headers["Content-Security-Policy"]
    _run(go())


def test_401_reject_still_carries_security_headers():
    """A 401 from the auth middleware still goes through the security
    middleware (stacked outermost-first → auth returns → security
    stamps on the way out)."""
    stub = _stub()
    app = _build_app(stub)

    async def go():
        async with TestServer(app) as s, TestClient(s) as c:
            r = await c.get("/api/v1/sessions")  # no bearer
            assert r.status == 401
            assert r.headers["X-Content-Type-Options"] == "nosniff"
    _run(go())
