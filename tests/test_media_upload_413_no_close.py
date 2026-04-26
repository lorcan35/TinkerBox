"""L1 regression test: POST /api/media/upload 413 must NOT carry
``Connection: close``.

Issue #121, refs #89, refs #94.

Pre-fix the Content-Length-too-big rejection forced the TCP
connection to drop, which is messy HTTP semantics — Tab5 doesn't
pipeline today but tearing the connection for a single 413 forces
a fresh handshake for the next request.

The fix removes the explicit ``Connection: close`` header from the
413 response.  This test pins the new contract end-to-end via
TestServer/TestClient so a future refactor can't quietly add the
header back.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from dragon_voice.api.media_routes import MediaRoutes


def _build_app() -> web.Application:
    """MediaRoutes with stubbed MediaStore — we never reach the body
    read because the Content-Length pre-check rejects first."""
    store = MagicMock()
    store.get_path = AsyncMock()
    routes = MediaRoutes(media_store=store, url_signer=None)
    app = web.Application(client_max_size=64 * 1024 * 1024)
    routes.register(app)
    return app


class Post413NoCloseHeaderTest(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_post_413_does_not_carry_connection_close(self) -> None:
        """Headline contract: oversized POST → 413 + NO `Connection:
        close` header.  Pre-fix this test would fail because the
        handler explicitly added the header."""
        async def go():
            app = _build_app()
            async with TestServer(app) as srv, TestClient(srv) as client:
                # Send a Content-Length declaration past the 10 MB cap
                # without actually sending that much body — the
                # pre-check fires on the declared length alone.
                fake_body = b"x" * 1024  # actual body small
                resp = await client.post(
                    "/api/media/upload",
                    data=fake_body,
                    headers={"Content-Length": str(20 * 1024 * 1024)},
                )
                self.assertEqual(resp.status, 413, "must reject as too large")
                connection_hdr = resp.headers.get("Connection", "").lower()
                # Either absent, or set by aiohttp's default keep-alive
                # logic (which is "keep-alive" on HTTP/1.1).  What we
                # MUST NOT see is an explicit "close" — that's what
                # L1 removes.
                self.assertNotEqual(
                    connection_hdr, "close",
                    "L1 fix not in place: response carries Connection: close"
                )

        self._run(go())

    def test_post_413_body_unchanged(self) -> None:
        """Regression guard: removing the header MUST NOT change the
        JSON body shape.  The response still includes error/max_bytes/
        declared_bytes for client-side debugging."""
        async def go():
            app = _build_app()
            async with TestServer(app) as srv, TestClient(srv) as client:
                resp = await client.post(
                    "/api/media/upload",
                    data=b"x" * 1024,
                    headers={"Content-Length": str(20 * 1024 * 1024)},
                )
                self.assertEqual(resp.status, 413)
                body = await resp.json()
                self.assertEqual(body["error"], "payload too large")
                self.assertIn("max_bytes", body)
                self.assertIn("declared_bytes", body)
                self.assertEqual(body["declared_bytes"], 20 * 1024 * 1024)

        self._run(go())


if __name__ == "__main__":
    unittest.main()
