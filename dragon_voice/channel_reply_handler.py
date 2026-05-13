"""W7-F channel_reply WS command handler (extracted from server.py).

Tab5 emits `channel_reply` frames (per TinkerTab PR #471, W7-E.4b real
voice-dictated reply path).  Dragon's job is to:

  1. Record the reply in the cross-session agent_log ring with
     source="user_reply" so it surfaces in Tab5's Agents overlay
     alongside Dragon + gateway tool calls.
  2. ACK back with a `channel_reply_ack` JSON frame containing
     ok=true + a stub `platform_message_id`.  Real platform
     forwarding (Telegram/WhatsApp/etc.) lands in W7-F.2 — needs
     the Python WS-RPC client to OpenClaw.

The handler is intentionally synchronous: the ACK fires in the same
tick as the receive, no async deferred state, and `record_call` +
`record_result` both run so the agent_log entry transitions
running→done atomically.

Extracted to allow standalone unit testing (see
`tests/test_channel_reply_handler.py`).  server.py's WS read loop
simply calls `handle_channel_reply(cmd, ws, ws_id, logger)`.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any, Protocol

from dragon_voice.api import agent_log


class _WSLike(Protocol):
    async def send_json(self, data: dict, **kwargs: Any) -> None: ...


def _make_platform_message_id() -> str:
    """Generate the stub:<hex> id used until real platform forwarding lands."""
    return f"stub:{secrets.token_hex(6)}"


async def handle_channel_reply(
    cmd: dict,
    ws: _WSLike,
    ws_id: str,
    logger: logging.Logger,
) -> dict:
    """Dispatch a single `channel_reply` WS command.

    Returns the ACK dict that was sent back to the client (also useful
    for tests that don't want to mock send_json).
    """
    ch = cmd.get("channel", "")
    thread = cmd.get("thread_id", "")
    text = cmd.get("text", "")
    in_reply_to = cmd.get("in_reply_to", "")
    logger.info(
        "channel_reply RX: ws=%s ch=%s thread=%s text=%.60s in_reply_to=%s",
        ws_id, ch, thread, text, in_reply_to,
    )

    platform_msg_id = _make_platform_message_id()
    agent_log.record_call(
        "channel_reply",
        {
            "channel": ch,
            "thread_id": thread,
            "text_preview": text[:80],
        },
        source="user_reply",
    )
    agent_log.record_result(
        "channel_reply",
        {
            "ok": True,
            "platform_message_id": platform_msg_id,
        },
        execution_ms=0,
        source="user_reply",
    )
    ack = {
        "type": "channel_reply_ack",
        "channel": ch,
        "thread_id": thread,
        "ok": True,
        "platform_message_id": platform_msg_id,
    }
    await ws.send_json(ack)
    return ack
