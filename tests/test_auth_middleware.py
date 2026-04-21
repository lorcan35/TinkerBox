"""Wave 13 C2: regression test for the REST bearer-token middleware.

Covers:
  1. Public prefixes (`/health`, `/ws/voice`, `/dashboard`, `/api/media/`)
     are reachable without any Authorization header.
  2. Private routes with no token       -> 401 missing_bearer_token
  3. Private routes with a wrong token  -> 401 invalid_bearer_token
  4. Private routes with the right one  -> 200 OK + handler runs
  5. OPTIONS preflight always passes    -> 200 (CORS answers first)
  6. Fail-closed: when api_token is blank -> 503 dragon_api_token_not_configured
     (this is the critical "deployer forgot to set DRAGON_API_TOKEN" case)

Run:
    python3 -m pytest tests/test_auth_middleware.py -v
    # or:
    python3 tests/test_auth_middleware.py
"""

import asyncio
import os
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from dragon_voice.config import load_config
from dragon_voice import server as srv_mod


class _StubServer(srv_mod.VoiceServer):
    """Subclass that bypasses the heavy VoiceServer constructor."""
    def __init__(self, config):
        self._config = config


def _build_app(stub: _StubServer) -> web.Application:
    async def _ok(request):
        return web.json_response({"ok": True})

    @web.middleware
    async def _mw(request, handler):
        return await stub._auth_middleware(request, handler)

    app = web.Application(middlewares=[_mw])
    for path in ("/health", "/ws/voice", "/dashboard", "/api/media/abc", "/api/v1/sessions"):
        app.router.add_get(path, _ok)
    app.router.add_options("/api/v1/sessions", _ok)
    return app


class AuthMiddlewareTests(unittest.TestCase):
    def setUp(self):
        os.environ["DRAGON_API_TOKEN"] = "wave13-c2-test-token"
        self.cfg = load_config()
        self.assertEqual(self.cfg.server.api_token, "wave13-c2-test-token")

    def _run(self, coro):
        return asyncio.run(coro)

    def test_public_prefixes_no_auth(self):
        stub = _StubServer(self.cfg)
        app = _build_app(stub)

        async def go():
            async with TestServer(app) as server, TestClient(server) as client:
                for path in ("/health", "/ws/voice", "/dashboard", "/api/media/abc"):
                    r = await client.get(path)
                    self.assertEqual(r.status, 200, f"{path} expected 200, got {r.status}")
        self._run(go())

    def test_private_route_missing_token(self):
        stub = _StubServer(self.cfg)
        app = _build_app(stub)

        async def go():
            async with TestServer(app) as server, TestClient(server) as client:
                r = await client.get("/api/v1/sessions")
                self.assertEqual(r.status, 401)
                body = await r.json()
                self.assertEqual(body["error"], "missing_bearer_token")
        self._run(go())

    def test_private_route_wrong_token(self):
        stub = _StubServer(self.cfg)
        app = _build_app(stub)

        async def go():
            async with TestServer(app) as server, TestClient(server) as client:
                r = await client.get("/api/v1/sessions", headers={"Authorization": "Bearer wrong"})
                self.assertEqual(r.status, 401)
                body = await r.json()
                self.assertEqual(body["error"], "invalid_bearer_token")
        self._run(go())

    def test_private_route_right_token(self):
        stub = _StubServer(self.cfg)
        app = _build_app(stub)

        async def go():
            async with TestServer(app) as server, TestClient(server) as client:
                r = await client.get(
                    "/api/v1/sessions",
                    headers={"Authorization": "Bearer wave13-c2-test-token"})
                self.assertEqual(r.status, 200)
        self._run(go())

    def test_options_preflight(self):
        stub = _StubServer(self.cfg)
        app = _build_app(stub)

        async def go():
            async with TestServer(app) as server, TestClient(server) as client:
                r = await client.options("/api/v1/sessions")
                self.assertEqual(r.status, 200)
        self._run(go())

    def test_fail_closed_when_token_unconfigured(self):
        self.cfg.server.api_token = ""
        stub = _StubServer(self.cfg)
        app = _build_app(stub)

        async def go():
            async with TestServer(app) as server, TestClient(server) as client:
                r = await client.get("/api/v1/sessions", headers={"Authorization": "Bearer any"})
                self.assertEqual(r.status, 503)
                body = await r.json()
                self.assertEqual(body["error"], "dragon_api_token_not_configured")
        self._run(go())


if __name__ == "__main__":
    unittest.main(verbosity=2)
