"""Tests for `dragon_voice.channels.MockConnector`.

The mock is the boot default until W7-F.2 lands.  We pin its
contract here so any future connector that subs in via
`channel_reply_handler.set_connector(...)` knows exactly what to
match:

  * `send_reply` is async + returns a `ChannelReplyResult`
  * `ok=true` always
  * `platform_message_id` follows `stub:<12 hex>` shape (so Tab5's
    existing W7-F UI never mistakes a stub for a real id — the
    `stub:` prefix is the giveaway)
  * Unique id per call (drives the `test_ack_platform_message_id_is_unique`
    case in test_channel_reply_handler)
  * Custom prefix supported (for future connectors that want the
    same shape but a different platform tag)
"""

from __future__ import annotations

import re
import unittest

from dragon_voice.channels import ChannelReplyResult, MockConnector


class TestMockConnector(unittest.IsolatedAsyncioTestCase):
    async def test_returns_ok_result(self):
        c = MockConnector()
        r = await c.send_reply(channel="tg", thread_id="t", text="hi")
        self.assertIsInstance(r, ChannelReplyResult)
        self.assertTrue(r.ok)
        self.assertEqual(r.error, "")

    async def test_platform_message_id_shape(self):
        c = MockConnector()
        r = await c.send_reply(channel="tg", thread_id="t", text="hi")
        # `stub:<12 hex>` — 6 bytes via secrets.token_hex(6) = 12 hex chars
        self.assertRegex(r.platform_message_id, r"^stub:[0-9a-f]{12}$")

    async def test_unique_id_per_call(self):
        c = MockConnector()
        ids = set()
        for _ in range(10):
            r = await c.send_reply(channel="x", thread_id="y", text="z")
            ids.add(r.platform_message_id)
        self.assertEqual(len(ids), 10)

    async def test_custom_prefix(self):
        c = MockConnector(prefix="recorded")
        r = await c.send_reply(channel="tg", thread_id="t", text="hi")
        self.assertTrue(r.platform_message_id.startswith("recorded:"))
        # Hex tail unchanged.
        self.assertRegex(r.platform_message_id, r"^recorded:[0-9a-f]{12}$")

    async def test_accepts_in_reply_to_optional(self):
        c = MockConnector()
        # in_reply_to defaults to "" — test both shapes still return ok.
        r1 = await c.send_reply(channel="tg", thread_id="t", text="hi")
        r2 = await c.send_reply(
            channel="tg", thread_id="t", text="hi", in_reply_to="ref"
        )
        self.assertTrue(r1.ok)
        self.assertTrue(r2.ok)


class TestChannelReplyResult(unittest.TestCase):
    def test_fields_default(self):
        r = ChannelReplyResult(ok=True, platform_message_id="x")
        self.assertEqual(r.error, "")

    def test_failure_carries_error(self):
        r = ChannelReplyResult(
            ok=False, platform_message_id="", error="auth failed"
        )
        self.assertFalse(r.ok)
        self.assertEqual(r.error, "auth failed")


if __name__ == "__main__":
    unittest.main()
