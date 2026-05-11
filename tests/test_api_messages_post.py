"""REST API tests for the W3-C-a add-message POST endpoint.

Wave 3-C-a (cross-stack cohesion audit 2026-05-11).  Tab5 POSTs SOLO
and ONBOARD turn pairs to ``POST /api/v1/sessions/{session_id}/messages``
so the Dragon DB becomes the canonical chat-message store across all
six voice modes.

Pattern matches ``tests/test_scheduler_api.py`` — real aiohttp
TestServer/TestClient + stub managers so we can assert HTTP status,
content-type, and body shape end-to-end.

Coverage (one test per branch):

  * happy path → 201 + full message dict
  * unknown session → 404
  * malformed JSON body → 400
  * non-object body → 400
  * missing role → 400
  * unknown role → 400
  * missing content → 400
  * empty content → 400
  * unknown input_mode → 400
  * optional fields (model, latency_ms, token_count) → forwarded to store
  * MessageStore raises → 500 with error body
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from dragon_voice.api.messages import MessageRoutes


def _build_app(*, session_exists: bool = True, add_raises: bool = False) -> tuple[web.Application, MagicMock, AsyncMock]:
    """Wire MessageRoutes against stub Database / SessionManager / MessageStore."""
    db = MagicMock()
    session_mgr = MagicMock()
    if session_exists:
        session_mgr.get_session = AsyncMock(return_value={"id": "sess_test"})
    else:
        session_mgr.get_session = AsyncMock(return_value=None)
    message_store = MagicMock()
    if add_raises:
        message_store.add_message = AsyncMock(side_effect=RuntimeError("db is on fire"))
    else:
        message_store.add_message = AsyncMock(side_effect=lambda **kwargs: {
            "id": "msg_abc123",
            "session_id": kwargs["session_id"],
            "role": kwargs["role"],
            "content": kwargs["content"],
            "input_mode": kwargs.get("input_mode", "text"),
            "model": kwargs.get("model"),
            "token_count": kwargs.get("token_count"),
            "latency_ms": kwargs.get("latency_ms"),
            "created_at": 1778519162.0,
        })
    app = web.Application()
    MessageRoutes(db=db, session_mgr=session_mgr, message_store=message_store).register(app)
    return app, session_mgr, message_store


class AddMessagePostTests(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    # ── Happy path ────────────────────────────────────────────────

    def test_post_creates_user_turn_201(self) -> None:
        app, _, store = _build_app()

        async def go():
            async with TestServer(app) as srv, TestClient(srv) as client:
                resp = await client.post(
                    "/api/v1/sessions/sess_test/messages",
                    json={"role": "user", "content": "Tell me a joke"},
                )
                self.assertEqual(resp.status, 201)
                self.assertEqual(resp.content_type, "application/json")
                body = await resp.json()
                self.assertEqual(body["id"], "msg_abc123")
                self.assertEqual(body["role"], "user")
                self.assertEqual(body["content"], "Tell me a joke")
                self.assertEqual(body["input_mode"], "text")
            store.add_message.assert_awaited_once()

        self._run(go())

    def test_post_assistant_turn_with_optional_fields(self) -> None:
        """All optional fields forward through to MessageStore.add_message."""
        app, _, store = _build_app()

        async def go():
            async with TestServer(app) as srv, TestClient(srv) as client:
                resp = await client.post(
                    "/api/v1/sessions/sess_test/messages",
                    json={
                        "role": "assistant",
                        "content": "Why did the ketchup blush?",
                        "input_mode": "voice",
                        "model": "openai/gpt-4o-audio-preview",
                        "token_count": 18,
                        "latency_ms": 5945.0,
                        "audio_duration_s": 4.33,
                    },
                )
                self.assertEqual(resp.status, 201)
            kwargs = store.add_message.await_args.kwargs
            self.assertEqual(kwargs["role"], "assistant")
            self.assertEqual(kwargs["input_mode"], "voice")
            self.assertEqual(kwargs["model"], "openai/gpt-4o-audio-preview")
            self.assertEqual(kwargs["token_count"], 18)
            self.assertEqual(kwargs["latency_ms"], 5945.0)
            self.assertEqual(kwargs["audio_duration_s"], 4.33)

        self._run(go())

    # ── Negative paths ────────────────────────────────────────────

    def test_post_unknown_session_404(self) -> None:
        app, _, _ = _build_app(session_exists=False)

        async def go():
            async with TestServer(app) as srv, TestClient(srv) as client:
                resp = await client.post(
                    "/api/v1/sessions/sess_nope/messages",
                    json={"role": "user", "content": "hi"},
                )
                self.assertEqual(resp.status, 404)

        self._run(go())

    def test_post_malformed_json_400(self) -> None:
        app, _, _ = _build_app()

        async def go():
            async with TestServer(app) as srv, TestClient(srv) as client:
                resp = await client.post(
                    "/api/v1/sessions/sess_test/messages",
                    data="not-json{[",
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(resp.status, 400)

        self._run(go())

    def test_post_non_object_body_400(self) -> None:
        app, _, _ = _build_app()

        async def go():
            async with TestServer(app) as srv, TestClient(srv) as client:
                resp = await client.post(
                    "/api/v1/sessions/sess_test/messages",
                    json=["not", "an", "object"],
                )
                self.assertEqual(resp.status, 400)

        self._run(go())

    def test_post_missing_role_400(self) -> None:
        app, _, _ = _build_app()

        async def go():
            async with TestServer(app) as srv, TestClient(srv) as client:
                resp = await client.post(
                    "/api/v1/sessions/sess_test/messages",
                    json={"content": "hi"},
                )
                self.assertEqual(resp.status, 400)

        self._run(go())

    def test_post_unknown_role_400(self) -> None:
        app, _, _ = _build_app()

        async def go():
            async with TestServer(app) as srv, TestClient(srv) as client:
                resp = await client.post(
                    "/api/v1/sessions/sess_test/messages",
                    json={"role": "narrator", "content": "hi"},
                )
                self.assertEqual(resp.status, 400)

        self._run(go())

    def test_post_missing_content_400(self) -> None:
        app, _, _ = _build_app()

        async def go():
            async with TestServer(app) as srv, TestClient(srv) as client:
                resp = await client.post(
                    "/api/v1/sessions/sess_test/messages",
                    json={"role": "user"},
                )
                self.assertEqual(resp.status, 400)

        self._run(go())

    def test_post_empty_content_400(self) -> None:
        app, _, _ = _build_app()

        async def go():
            async with TestServer(app) as srv, TestClient(srv) as client:
                resp = await client.post(
                    "/api/v1/sessions/sess_test/messages",
                    json={"role": "user", "content": ""},
                )
                self.assertEqual(resp.status, 400)

        self._run(go())

    def test_post_unknown_input_mode_400(self) -> None:
        app, _, _ = _build_app()

        async def go():
            async with TestServer(app) as srv, TestClient(srv) as client:
                resp = await client.post(
                    "/api/v1/sessions/sess_test/messages",
                    json={"role": "user", "content": "hi", "input_mode": "morse"},
                )
                self.assertEqual(resp.status, 400)

        self._run(go())

    def test_post_store_raises_returns_500(self) -> None:
        app, _, _ = _build_app(add_raises=True)

        async def go():
            async with TestServer(app) as srv, TestClient(srv) as client:
                resp = await client.post(
                    "/api/v1/sessions/sess_test/messages",
                    json={"role": "user", "content": "hi"},
                )
                self.assertEqual(resp.status, 500)
                body = await resp.json()
                self.assertIn("Failed to add message", body.get("error", ""))

        self._run(go())


if __name__ == "__main__":
    unittest.main()
