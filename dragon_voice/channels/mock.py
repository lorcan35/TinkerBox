"""In-process mock connector — boot default until W7-F.2 lands."""

from __future__ import annotations

import logging
import secrets

from dragon_voice.channels.base import ChannelReplyResult

logger = logging.getLogger(__name__)


class MockConnector:
    """No-op connector for dev + tests.

    Mints a `stub:<hex>` platform_message_id and returns ok=true.
    The actual reply text never leaves Dragon — useful for exercising
    the Tab5↔Dragon round-trip without real platform credentials.

    Substitute with a real connector (gateway WS-RPC, direct
    Telegram, etc.) in production once W7-F.2 lands.
    """

    def __init__(self, *, prefix: str = "stub") -> None:
        self._prefix = prefix

    async def send_reply(
        self,
        channel: str,
        thread_id: str,
        text: str,
        in_reply_to: str = "",
    ) -> ChannelReplyResult:
        pmid = f"{self._prefix}:{secrets.token_hex(6)}"
        logger.debug(
            "MockConnector: channel=%s thread=%s text=%.40s → %s",
            channel, thread_id, text, pmid,
        )
        return ChannelReplyResult(ok=True, platform_message_id=pmid)
