"""Unit tests for WS upgrade rejection JSON shape (γ3-Dragon).

Issue #111, refs #89, refs #101.  Pre-fix the WS upgrade endpoint
returned plain-text bodies for 401 (bad token) and 503 (capacity).
Tooling that wanted to programmatically distinguish "auth failed"
from "server full" had to substring-match the prose.

The fix swaps both responses for ``web.json_response`` carrying a
machine-readable ``code`` plus a user-friendly ``message`` (matching
the γ1/γ2 structured-error pattern).

Coverage:
  * 401 body parses as JSON, contains ``code="auth_failed"``
  * 503 body parses as JSON, contains ``code="server_full"``
  * Content-Type is application/json (so HTTP clients don't treat
    the body as plain text)
  * Status codes themselves are unchanged (Tab5's
    esp_websocket_client uses the raw status code for its stop-retry
    decision — that contract MUST stay intact)

Tests run against an actual aiohttp app via TestServer/TestClient
so the JSON body + Content-Type + status code are all verified
end-to-end at the HTTP layer.  Same pattern as test_auth_middleware.py.
"""
from __future__ import annotations

import asyncio
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from dragon_voice.config import VoiceConfig
from dragon_voice.server import VoiceServer


def _make_server(token: str = "the-good-token", max_connections: int = 8) -> VoiceServer:
    """Build a minimal VoiceServer wired enough for ``_handle_ws_voice``
    to reach the auth + capacity gates.  We never get past those in
    these tests so most subsystems stay None."""
    s = VoiceServer.__new__(VoiceServer)  # type: ignore[call-arg]
    cfg = VoiceConfig()
    cfg.server.api_token = token
    s._config = cfg
    s._active_connections = {}
    s._max_connections = max_connections
    return s


def _build_app(server: VoiceServer) -> web.Application:
    app = web.Application()
    app.router.add_get("/ws/voice", server._handle_ws_voice)
    return app


_WS_HANDSHAKE_HEADERS = {
    "Connection": "Upgrade",
    "Upgrade": "websocket",
    "Sec-WebSocket-Version": "13",
    "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
}


class WsUpgradeErrorTests(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_bad_token_returns_json_with_auth_failed(self) -> None:
        """Bad bearer → 401 with structured JSON body."""
        server = _make_server(token="the-good-token")
        app = _build_app(server)

        async def go():
            async with TestServer(app) as srv, TestClient(srv) as client:
                r = await client.get(
                    "/ws/voice",
                    headers={
                        "Authorization": "Bearer nope-wrong-token",
                        **_WS_HANDSHAKE_HEADERS,
                    },
                )
                self.assertEqual(r.status, 401, "status code is the contract Tab5 reads")
                self.assertEqual(
                    r.content_type, "application/json",
                    "JSON body required so dashboards/ops can introspect",
                )
                body = await r.json()
                self.assertEqual(body["code"], "auth_failed")
                self.assertIn("message", body)
                # Implementation-detail leakage guard
                msg = body["message"].lower()
                self.assertNotIn("hmac", msg)
                self.assertNotIn("compare_digest", msg)
                # And no raw "Unauthorized" — message must point at the fix
                self.assertNotEqual(msg, "unauthorized")
        self._run(go())

    def test_missing_header_also_returns_auth_failed(self) -> None:
        """No Authorization header at all → same 401/auth_failed shape.
        Pre-fix this case also returned text="Unauthorized" — pin the
        JSON shape so a future refactor can't silently regress one
        of the two paths back to plain text."""
        server = _make_server(token="the-good-token")
        app = _build_app(server)

        async def go():
            async with TestServer(app) as srv, TestClient(srv) as client:
                r = await client.get("/ws/voice", headers=_WS_HANDSHAKE_HEADERS)
                self.assertEqual(r.status, 401)
                body = await r.json()
                self.assertEqual(body["code"], "auth_failed")
        self._run(go())

    def test_at_capacity_returns_server_full_json(self) -> None:
        """Connection limit reached → 503 with structured JSON body."""
        server = _make_server(token="the-good-token", max_connections=2)
        # Pre-fill past cap
        server._active_connections = {
            "ws_a": {"device_id": "x"},
            "ws_b": {"device_id": "y"},
        }
        app = _build_app(server)

        async def go():
            async with TestServer(app) as srv, TestClient(srv) as client:
                r = await client.get(
                    "/ws/voice",
                    headers={
                        "Authorization": "Bearer the-good-token",
                        **_WS_HANDSHAKE_HEADERS,
                    },
                )
                self.assertEqual(r.status, 503)
                self.assertEqual(r.content_type, "application/json")
                body = await r.json()
                self.assertEqual(body["code"], "server_full")
                self.assertIn("message", body)
                # No raw "Too many connections" prose leak
                self.assertNotIn("too many", body["message"].lower())
        self._run(go())

    def test_status_codes_unchanged_for_tab5_stop_retry(self) -> None:
        """Tab5's esp_websocket_client reads the HTTP status code and
        counts auth failures off it (γ3-Tab5).  This test pins the
        contract: 401 stays 401, 503 stays 503 — no accidental drift
        to 400/500 just because we restructured the body shape."""
        server = _make_server(token="the-good-token", max_connections=1)
        server._active_connections = {"ws_a": {"device_id": "x"}}
        app = _build_app(server)

        async def go():
            async with TestServer(app) as srv, TestClient(srv) as client:
                bad = await client.get(
                    "/ws/voice",
                    headers={"Authorization": "Bearer wrong", **_WS_HANDSHAKE_HEADERS},
                )
                self.assertEqual(bad.status, 401)

                full = await client.get(
                    "/ws/voice",
                    headers={
                        "Authorization": "Bearer the-good-token",
                        **_WS_HANDSHAKE_HEADERS,
                    },
                )
                self.assertEqual(full.status, 503)
        self._run(go())


if __name__ == "__main__":
    unittest.main()
