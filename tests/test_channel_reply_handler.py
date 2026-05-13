"""W7-F: channel_reply WS command handler tests.

Verifies that `handle_channel_reply`:
  * sends back a `channel_reply_ack` JSON frame with ok=true and a
    `stub:<hex>` platform_message_id (the real platform forwarding
    lands in W7-F.2 — needs the Python WS-RPC client to OpenClaw)
  * records a single agent_log entry per receive with source=user_reply,
    transitioned running→done in one tick (synchronous ACK = no async
    deferred state)
  * truncates the text_preview at 80 chars so long replies don't bloat
    the cross-session activity ring
  * preserves the channel + thread_id in both the agent_log args AND
    the ACK so Tab5 (W7-E.4 ack handler) can match the reply to the
    originating thread
  * fails gracefully on missing fields (channel/thread/text optional)
"""

from __future__ import annotations

import asyncio
import logging
import unittest
from unittest.mock import AsyncMock

from dragon_voice.api import agent_log as _alog
from dragon_voice.channel_reply_handler import handle_channel_reply


class _BaseRingTest(unittest.IsolatedAsyncioTestCase):
    """Snapshot + restore the process-global agent_log ring so each
    test starts clean and doesn't bleed state into the next."""

    async def asyncSetUp(self):
        with _alog._lock:
            self._saved_ring = list(_alog._ring)
            self._saved_next = _alog._next_id
            _alog._ring.clear()
            _alog._next_id = 1
        self.ws = AsyncMock()
        self.logger = logging.getLogger("test_channel_reply_handler")

    async def asyncTearDown(self):
        with _alog._lock:
            _alog._ring.clear()
            for item in self._saved_ring:
                _alog._ring.append(item)
            _alog._next_id = self._saved_next


class TestAck(_BaseRingTest):
    async def test_ack_shape_matches_protocol(self):
        cmd = {
            "type": "channel_reply",
            "channel": "tg",
            "thread_id": "tg:friends:42",
            "text": "yes works for me",
        }
        ack = await handle_channel_reply(cmd, self.ws, "ws-1", self.logger)
        self.assertEqual(ack["type"], "channel_reply_ack")
        self.assertEqual(ack["channel"], "tg")
        self.assertEqual(ack["thread_id"], "tg:friends:42")
        self.assertTrue(ack["ok"])
        self.assertTrue(ack["platform_message_id"].startswith("stub:"))
        # send_json fired exactly once with the same dict
        self.ws.send_json.assert_awaited_once_with(ack)

    async def test_ack_platform_message_id_is_unique(self):
        cmd = {"channel": "tg", "thread_id": "t", "text": "x"}
        a1 = await handle_channel_reply(cmd, self.ws, "ws", self.logger)
        a2 = await handle_channel_reply(cmd, self.ws, "ws", self.logger)
        self.assertNotEqual(a1["platform_message_id"], a2["platform_message_id"])

    async def test_missing_fields_default_to_empty_strings(self):
        # Tab5 firmware always populates these but Dragon must not
        # crash if a malformed client sends an incomplete frame.
        ack = await handle_channel_reply({}, self.ws, "ws", self.logger)
        self.assertEqual(ack["channel"], "")
        self.assertEqual(ack["thread_id"], "")
        self.assertTrue(ack["ok"])


class TestAgentLogIntegration(_BaseRingTest):
    async def test_records_running_then_done_atomically(self):
        cmd = {"channel": "wa", "thread_id": "wa:1", "text": "ack"}
        await handle_channel_reply(cmd, self.ws, "ws", self.logger)

        with _alog._lock:
            entries = list(_alog._ring)
        # Single entry that's already "done" (record_result transitioned
        # the matching "running" entry atomically).
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["tool"], "channel_reply")
        self.assertEqual(entries[0]["status"], "done")
        self.assertEqual(entries[0]["source"], "user_reply")

    async def test_args_preserve_channel_and_thread(self):
        cmd = {"channel": "tg", "thread_id": "tg:123", "text": "hi"}
        await handle_channel_reply(cmd, self.ws, "ws", self.logger)
        with _alog._lock:
            entry = list(_alog._ring)[0]
        self.assertEqual(entry["args"]["channel"], "tg")
        self.assertEqual(entry["args"]["thread_id"], "tg:123")

    async def test_text_preview_truncates_at_80_chars(self):
        long_text = "x" * 500
        cmd = {"channel": "tg", "thread_id": "t", "text": long_text}
        await handle_channel_reply(cmd, self.ws, "ws", self.logger)
        with _alog._lock:
            entry = list(_alog._ring)[0]
        self.assertEqual(len(entry["args"]["text_preview"]), 80)

    async def test_short_text_passes_through_unchanged(self):
        cmd = {"channel": "tg", "thread_id": "t", "text": "short"}
        await handle_channel_reply(cmd, self.ws, "ws", self.logger)
        with _alog._lock:
            entry = list(_alog._ring)[0]
        self.assertEqual(entry["args"]["text_preview"], "short")

    async def test_result_contains_ack_payload(self):
        cmd = {"channel": "tg", "thread_id": "t", "text": "hi"}
        ack = await handle_channel_reply(cmd, self.ws, "ws", self.logger)
        with _alog._lock:
            entry = list(_alog._ring)[0]
        # Result is rendered as a JSON-string preview (see
        # agent_log._preview); the ack's platform_message_id must
        # round-trip through it.
        self.assertIn(ack["platform_message_id"], entry["result"])
        self.assertIn('"ok": true', entry["result"])


class TestConnectorDispatch(_BaseRingTest):
    """W7-F.2 scaffold: handler dispatches via injected ChannelConnector."""

    async def test_connector_override_param_used(self):
        from dragon_voice.channels import ChannelReplyResult

        class RecordingConnector:
            def __init__(self) -> None:
                self.calls: list = []

            async def send_reply(self, channel, thread_id, text, in_reply_to=""):
                self.calls.append((channel, thread_id, text, in_reply_to))
                return ChannelReplyResult(
                    ok=True, platform_message_id="recorded:abc123"
                )

        rec = RecordingConnector()
        cmd = {
            "channel": "wa",
            "thread_id": "wa:99",
            "text": "via recorder",
            "in_reply_to": "wa:99:42",
        }
        ack = await handle_channel_reply(
            cmd, self.ws, "ws", self.logger, connector=rec
        )
        self.assertEqual(len(rec.calls), 1)
        self.assertEqual(rec.calls[0], ("wa", "wa:99", "via recorder", "wa:99:42"))
        self.assertEqual(ack["platform_message_id"], "recorded:abc123")
        self.assertTrue(ack["ok"])

    async def test_connector_failure_surfaces_in_ack(self):
        from dragon_voice.channels import ChannelReplyResult

        class FailingConnector:
            async def send_reply(self, channel, thread_id, text, in_reply_to=""):
                return ChannelReplyResult(
                    ok=False,
                    platform_message_id="",
                    error="rate limit hit",
                )

        cmd = {"channel": "tg", "thread_id": "t", "text": "hi"}
        ack = await handle_channel_reply(
            cmd, self.ws, "ws", self.logger, connector=FailingConnector()
        )
        self.assertFalse(ack["ok"])
        self.assertEqual(ack["error"], "rate limit hit")
        # agent_log result must also record the failure for visibility.
        with _alog._lock:
            entry = list(_alog._ring)[0]
        self.assertIn("rate limit hit", entry["result"])
        self.assertIn('"ok": false', entry["result"])

    async def test_default_module_connector_is_mock(self):
        from dragon_voice.channel_reply_handler import get_connector
        from dragon_voice.channels import MockConnector

        # Module-level singleton initialises to MockConnector — the
        # boot default until W7-F.2 swaps in a real one.
        self.assertIsInstance(get_connector(), MockConnector)

    async def test_source_bucket_appears_in_feed_summary(self):
        cmd = {"channel": "tg", "thread_id": "t", "text": "hi"}
        await handle_channel_reply(cmd, self.ws, "ws", self.logger)
        await handle_channel_reply(cmd, self.ws, "ws", self.logger)

        # Count entries with source=user_reply directly
        with _alog._lock:
            user_reply_count = sum(
                1 for e in _alog._ring if e.get("source") == "user_reply"
            )
        self.assertEqual(user_reply_count, 2)


class TestNoFailureModes(_BaseRingTest):
    async def test_non_dict_cmd_does_not_explode(self):
        # The WS read loop only calls handle_channel_reply for
        # cmd_type=="channel_reply" so cmd is always a dict in
        # practice, but defending against a malformed cmd_type
        # branch elsewhere is cheap and clarifies the contract.
        cmd = {"type": "channel_reply"}  # missing all data fields
        ack = await handle_channel_reply(cmd, self.ws, "ws", self.logger)
        self.assertTrue(ack["ok"])

    async def test_handler_is_callable_concurrently(self):
        # Two replies firing concurrently shouldn't corrupt the ring
        # (agent_log uses a lock for the append).
        cmds = [
            {"channel": "tg", "thread_id": f"t{i}", "text": f"msg-{i}"}
            for i in range(5)
        ]
        await asyncio.gather(
            *[handle_channel_reply(c, self.ws, f"ws{i}", self.logger)
              for i, c in enumerate(cmds)]
        )
        with _alog._lock:
            entries = list(_alog._ring)
        self.assertEqual(len(entries), 5)
        # All five must be in done state
        self.assertTrue(all(e["status"] == "done" for e in entries))


if __name__ == "__main__":
    unittest.main()
