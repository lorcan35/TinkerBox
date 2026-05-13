"""Abstract base for channel connectors.

A `ChannelConnector` represents a single outbound transport for
Tab5-originated replies.  Real implementations (gateway WS-RPC,
direct Telegram bot, etc.) plug in here and the channel_reply
handler dispatches through them.

The abstraction is intentionally minimal — one async method,
one result dataclass.  Future capabilities (delivery receipts,
typing indicators, attachment uploads) get added when there's
a second concrete connector that actually needs them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class ChannelReplyResult:
    """Outcome of dispatching a reply to a connector.

    The fields mirror what Tab5's `channel_reply_ack` JSON frame
    expects so the handler can pass them straight through to
    `ws.send_json` without translation.
    """

    ok: bool
    platform_message_id: str
    error: str = ""


class ChannelConnector(Protocol):
    """Outbound transport for a single user-dictated reply.

    Implementations:
      * `MockConnector` — boot-default, returns ok=true with a
        `stub:<hex>` platform_message_id.  No network I/O.
      * `GatewayConnector` (W7-F.2) — forwards via OpenClaw WS-RPC.
      * Direct connectors (Telegram, WhatsApp, etc.) — bypass the
        gateway when a platform-specific token is configured.

    `channel` is the canonical short name Tab5 emits (`tg`, `wa`,
    `dc`, etc.).  Implementations are responsible for mapping that
    to a platform-specific identifier.
    """

    async def send_reply(
        self,
        channel: str,
        thread_id: str,
        text: str,
        in_reply_to: str = "",
    ) -> ChannelReplyResult: ...
