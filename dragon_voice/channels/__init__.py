"""Channel connector abstraction.

Tab5 emits `channel_reply` frames; Dragon's job is to forward them to
the actual messaging platform (Telegram, WhatsApp, Discord, etc.).
The forwarding step is currently a stub (W7-F PR #305) — this package
sets up the swap-in seam so W7-F.2 (real gateway WS-RPC client) can
replace the mock without touching the WS dispatcher in server.py.

Design rules:
  * `ChannelConnector` is an abstract Protocol — no required state,
    one async method `send_reply` returning `ChannelReplyResult`.
  * `MockConnector` is the boot-default — logs + returns ok=true with
    a `stub:<hex>` platform_message_id (matches what the inline W7-F
    stub does today).  Useful for dev + tests.
  * Future connectors (`GatewayConnector`, direct `TelegramConnector`)
    live as sibling modules so the import surface stays clean.

Usage:
    from dragon_voice.channels import MockConnector, ChannelReplyResult

    connector = MockConnector()
    result = await connector.send_reply(
        channel="tg",
        thread_id="tg:thread:42",
        text="ok, see you Sunday",
    )
    assert result.ok
"""

from dragon_voice.channels.base import (
    ChannelConnector,
    ChannelReplyResult,
)
from dragon_voice.channels.mock import MockConnector

__all__ = [
    "ChannelConnector",
    "ChannelReplyResult",
    "MockConnector",
]
