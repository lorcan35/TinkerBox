"""W7-F symmetry tests: outbound channel_message push.

Verifies that POST /api/v1/debug/channel_message:
  * resolves device_id / session_id to a connected ws via the active-
    conn-registry callback (mirrors video_inject's structure)
  * sends a JSON frame with type=channel_message and the rest of the
    body forwarded verbatim (so callers can't accidentally push some
    other type through this endpoint)
  * records the push in the cross-session agent_log feed with
    source="channel_push" so Tab5's Agents overlay surfaces it
    alongside Dragon + gateway + user_reply entries
  * returns 400 when neither device_id nor session_id is supplied
  * returns 400 when channel field is missing (the one required body
    field for the Tab5-side router gate)
  * returns 404 when no connection matches
  * returns 410 when the matching ws was closed mid-flight

Together with `test_channel_reply_handler.py` this pins both halves
of the W7-F bidirectional channel surface.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from dragon_voice.api import agent_log as _alog
from dragon_voice.api.debug_channel import DebugChannelRoutes


class _BaseRingTest(AioHTTPTestCase):
    """Snapshot + restore the process-global agent_log ring so each
    test starts clean, just like test_channel_reply_handler does."""

    async def asyncSetUp(self):
        with _alog._lock:
            self._saved_ring = list(_alog._ring)
            self._saved_next = _alog._next_id
            _alog._ring.clear()
            _alog._next_id = 1
        # Per-test fake connection registry, populated by helpers below.
        self._conns: dict = {}
        await super().asyncSetUp()

    async def asyncTearDown(self):
        await super().asyncTearDown()
        with _alog._lock:
            _alog._ring.clear()
            for item in self._saved_ring:
                _alog._ring.append(item)
            _alog._next_id = self._saved_next

    async def get_application(self) -> web.Application:
        app = web.Application()
        DebugChannelRoutes(lambda: self._conns).register(app)
        return app

    def _add_conn(self, ws_id: str, device_id: str = "", session_id: str = "",
                  closed: bool = False) -> MagicMock:
        """Inject a fake conn into the registry.  Returns the ws mock."""
        ws = MagicMock()
        ws.closed = closed
        ws.send_json = AsyncMock()
        conn = {"device_id": device_id, "session_id": session_id, "ws": ws}
        self._conns[ws_id] = conn
        return ws


class TestPushHappyPath(_BaseRingTest):
    async def test_send_succeeds_and_returns_metadata(self):
        ws = self._add_conn("ws1", device_id="dev-A", session_id="sess-A")
        resp = await self.client.post(
            "/api/v1/debug/channel_message?device_id=dev-A",
            json={
                "channel": "tg",
                "sender": {"display_name": "Mom"},
                "preview": "dinner Sunday?",
                "priority": "high",
            },
        )
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertTrue(body["sent"])
        self.assertEqual(body["channel"], "tg")
        self.assertEqual(body["device_id"], "dev-A")
        # send_json fired once with a channel_message frame
        ws.send_json.assert_awaited_once()
        sent_frame = ws.send_json.call_args[0][0]
        self.assertEqual(sent_frame["type"], "channel_message")
        self.assertEqual(sent_frame["channel"], "tg")
        self.assertEqual(sent_frame["preview"], "dinner Sunday?")

    async def test_session_id_routing(self):
        ws = self._add_conn("ws1", device_id="d", session_id="match-this")
        resp = await self.client.post(
            "/api/v1/debug/channel_message?session_id=match-this",
            json={"channel": "wa", "sender": {"display_name": "S"}},
        )
        self.assertEqual(resp.status, 200)
        ws.send_json.assert_awaited_once()

    async def test_type_field_is_forced_to_channel_message(self):
        # Caller cannot accidentally push some other frame shape through
        # this endpoint (defense-in-depth — the route was designed
        # specifically for synthetic channel_message frames).
        ws = self._add_conn("ws1", device_id="d")
        await self.client.post(
            "/api/v1/debug/channel_message?device_id=d",
            json={"type": "config_update", "channel": "tg",
                  "voice_mode": 2},
        )
        sent = ws.send_json.call_args[0][0]
        self.assertEqual(sent["type"], "channel_message")
        # Other arbitrary fields are forwarded so future Dragon
        # additions (like ts, metadata) don't need a code change here.
        self.assertEqual(sent["voice_mode"], 2)


class TestPushAgentLogIntegration(_BaseRingTest):
    async def test_records_push_in_agent_log(self):
        self._add_conn("ws1", device_id="dev-A")
        await self.client.post(
            "/api/v1/debug/channel_message?device_id=dev-A",
            json={
                "channel": "tg",
                "sender": {"display_name": "Mom"},
                "preview": "dinner Sunday?",
                "priority": "high",
            },
        )
        with _alog._lock:
            entries = list(_alog._ring)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["tool"], "channel_message_push")
        self.assertEqual(entry["source"], "channel_push")
        self.assertEqual(entry["status"], "done")
        self.assertEqual(entry["args"]["channel"], "tg")
        self.assertEqual(entry["args"]["sender"], "Mom")
        self.assertEqual(entry["args"]["priority"], "high")
        self.assertIn("dinner", entry["args"]["preview"])

    async def test_preview_falls_back_to_text_field(self):
        # When `preview` is missing, the recorder falls back to `text`
        # (mirrors Tab5's ui_notification router preference).
        self._add_conn("ws1", device_id="d")
        await self.client.post(
            "/api/v1/debug/channel_message?device_id=d",
            json={
                "channel": "wa",
                "sender": {"display_name": "X"},
                "text": "fallback body",
            },
        )
        with _alog._lock:
            entry = list(_alog._ring)[0]
        self.assertEqual(entry["args"]["preview"], "fallback body")

    async def test_preview_truncates_at_80_chars(self):
        self._add_conn("ws1", device_id="d")
        await self.client.post(
            "/api/v1/debug/channel_message?device_id=d",
            json={"channel": "tg", "sender": {"display_name": "X"},
                  "preview": "y" * 500},
        )
        with _alog._lock:
            entry = list(_alog._ring)[0]
        self.assertEqual(len(entry["args"]["preview"]), 80)

    async def test_result_records_target_device_id(self):
        self._add_conn("ws1", device_id="dev-B")
        await self.client.post(
            "/api/v1/debug/channel_message?device_id=dev-B",
            json={"channel": "tg", "sender": {"display_name": "X"}},
        )
        with _alog._lock:
            entry = list(_alog._ring)[0]
        # result is rendered as a JSON-string preview; dev-B must round-trip.
        self.assertIn("dev-B", entry["result"])
        self.assertIn('"ok": true', entry["result"])

    async def test_failed_route_does_not_pollute_ring(self):
        # No matching connection → 404, no agent_log entry.
        resp = await self.client.post(
            "/api/v1/debug/channel_message?device_id=does-not-exist",
            json={"channel": "tg"},
        )
        self.assertEqual(resp.status, 404)
        with _alog._lock:
            entries = list(_alog._ring)
        self.assertEqual(entries, [])


class TestPushErrorPaths(_BaseRingTest):
    async def test_missing_id_query_returns_400(self):
        resp = await self.client.post(
            "/api/v1/debug/channel_message",
            json={"channel": "tg"},
        )
        self.assertEqual(resp.status, 400)
        body = await resp.json()
        self.assertIn("required", body["error"])

    async def test_missing_channel_returns_400(self):
        self._add_conn("ws1", device_id="d")
        resp = await self.client.post(
            "/api/v1/debug/channel_message?device_id=d",
            json={"sender": {"display_name": "X"}},  # no `channel`
        )
        self.assertEqual(resp.status, 400)

    async def test_invalid_json_body_returns_400(self):
        self._add_conn("ws1", device_id="d")
        resp = await self.client.post(
            "/api/v1/debug/channel_message?device_id=d",
            data="not json",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status, 400)

    async def test_unmatched_target_returns_404(self):
        self._add_conn("ws1", device_id="dev-A")
        resp = await self.client.post(
            "/api/v1/debug/channel_message?device_id=dev-OTHER",
            json={"channel": "tg"},
        )
        self.assertEqual(resp.status, 404)

    async def test_closed_ws_returns_410(self):
        self._add_conn("ws1", device_id="d", closed=True)
        resp = await self.client.post(
            "/api/v1/debug/channel_message?device_id=d",
            json={"channel": "tg"},
        )
        self.assertEqual(resp.status, 410)

    async def test_non_object_body_returns_400(self):
        self._add_conn("ws1", device_id="d")
        resp = await self.client.post(
            "/api/v1/debug/channel_message?device_id=d",
            data=json.dumps(["not", "an", "object"]),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status, 400)


if __name__ == "__main__":
    unittest.main()
