"""W7-F channel_reply WS command handler.

Tab5 emits `channel_reply` frames (per TinkerTab PR #471, W7-E.4b real
voice-dictated reply path).  Dragon's job is to:

  1. Forward the reply via a `ChannelConnector` (boot default:
     `MockConnector` — stub:<hex> message id, no network I/O).
     W7-F.2 will swap in a real gateway WS-RPC connector or per-
     platform direct connectors without touching this handler.

  2. Record the reply in the cross-session agent_log ring with
     source="user_reply" so it surfaces in Tab5's Agents overlay
     alongside Dragon + gateway tool calls.

  3. ACK back with a `channel_reply_ack` JSON frame carrying the
     connector's result (ok / platform_message_id / error).

The handler is intentionally synchronous (with respect to the WS-task):
record_call + record_result both fire so the agent_log entry
transitions running→done in one tick, and the ACK ships before the
next WS frame is read.

Extracted to allow standalone unit testing (see
`tests/test_channel_reply_handler.py`).  server.py's WS read loop
calls `handle_channel_reply(cmd, ws, ws_id, logger)` and the handler
uses a module-level connector singleton — tests override via
`set_connector` so the default mock can be swapped for a recording
fake.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Protocol

from dragon_voice.api import agent_log
from dragon_voice.channels import ChannelConnector, MockConnector


class _WSLike(Protocol):
    async def send_json(self, data: dict, **kwargs: Any) -> None: ...


# Module-level connector singleton.  server.py's startup chain can
# override via `set_connector(GatewayConnector(...))` when W7-F.2
# lands; until then the mock is the boot default.
_connector: ChannelConnector = MockConnector()


def set_connector(connector: ChannelConnector) -> None:
    """Replace the active connector.

    Used by:
      * server.py startup (when a non-mock connector is configured)
      * tests/test_channel_reply_handler.py (inject recording fakes)
    """
    global _connector
    _connector = connector


def get_connector() -> ChannelConnector:
    """Return the active connector — handy for tests to assert on."""
    return _connector


async def handle_channel_reply(
    cmd: dict,
    ws: _WSLike,
    ws_id: str,
    logger: logging.Logger,
    connector: Optional[ChannelConnector] = None,
) -> dict:
    """Dispatch a single `channel_reply` WS command.

    `connector` overrides the module-level singleton for this call
    only — useful for tests that want isolation without `set_connector`.

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

    # Forward via the active connector.  Mock returns ok=true; real
    # connectors might fail (network, auth, rate limit) — the result
    # carries ok + error so the ACK + agent_log entry tell the truth.
    active = connector if connector is not None else _connector
    result = await active.send_reply(
        channel=ch, thread_id=thread, text=text, in_reply_to=in_reply_to
    )

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
            "ok": result.ok,
            "platform_message_id": result.platform_message_id,
            "error": result.error,
        },
        execution_ms=0,
        source="user_reply",
    )
    ack: dict = {
        "type": "channel_reply_ack",
        "channel": ch,
        "thread_id": thread,
        "ok": result.ok,
        "platform_message_id": result.platform_message_id,
    }
    if not result.ok and result.error:
        ack["error"] = result.error
    await ws.send_json(ack)
    return ack
