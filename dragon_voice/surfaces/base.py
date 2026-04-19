"""Tab5Surface — skill-facing facade for the Widget Platform.

Skills call these methods to push typed widget state to Tab5 (and future
devices). All methods are async and idempotent — same card_id is treated
as an update; new card_id = new widget.

Usage (from a skill):

    async def start(self, minutes):
        await self.surface.live(
            title="Deep work",
            body=f"{minutes}:00 remaining",
            icon="briefcase",
            tone="calm",
            progress=0.0,
            action=("PAUSE", "ts.pause"),
            priority=80,
            card_id="ts_25",
        )

See TinkerTab/docs/WIDGETS.md §5 for the full author experience.
"""
from __future__ import annotations
import asyncio
import logging
import uuid
from typing import Any, Awaitable, Callable, Optional, Tuple

log = logging.getLogger("tab5.surface")

# Type for the ws-send callable the surface uses. aiohttp WebSocketResponse.send_json
# is async and raises on closed sockets; caller handles that.
SendJson = Callable[[dict], Awaitable[Any]]

Action = Tuple[str, str]  # (label, event)


def _gen_card_id(skill_id: str | None = None) -> str:
    return f"{skill_id or 'widget'}_{uuid.uuid4().hex[:8]}"


class Tab5Surface:
    """Per-session facade. One instance per connected Tab5."""

    def __init__(self, send_json: SendJson, skill_id: str = "unknown") -> None:
        self._send = send_json
        self._skill_id = skill_id
        # Track live card_ids emitted by this surface so we can clear() later.
        self._live_cards: set[str] = set()

    def for_skill(self, skill_id: str) -> "Tab5Surface":
        """Return a child surface tagged with a specific skill id. The
        underlying send is shared; only the default skill_id differs."""
        child = Tab5Surface(self._send, skill_id=skill_id)
        child._live_cards = self._live_cards
        return child

    # ── widget_live ──────────────────────────────────────────────
    async def live(
        self,
        *,
        title: str,
        body: str,
        tone: str = "active",
        icon: Optional[str] = None,
        progress: Optional[float] = None,
        action: Optional[Action] = None,
        priority: int = 50,
        expires_ms: Optional[int] = None,
        card_id: Optional[str] = None,
        skill_id: Optional[str] = None,
    ) -> str:
        """Create/replace the live widget. Returns the card_id for later updates."""
        sid = skill_id or self._skill_id
        cid = card_id or _gen_card_id(sid)
        msg: dict = {
            "type": "widget_live",
            "skill_id": sid,
            "card_id": cid,
            "title": title[:63],
            "body": body[:255],
            "tone": tone,
            "priority": max(0, min(100, int(priority))),
        }
        if icon:
            msg["icon"] = icon[:15]
        if progress is not None:
            msg["progress"] = float(max(0.0, min(1.0, progress)))
        if action:
            msg["action"] = {"label": action[0][:15], "event": action[1][:47]}
        if expires_ms:
            msg["expires_ms"] = int(expires_ms)
        await self._safe_send(msg, describe=f"live {sid}/{cid}")
        self._live_cards.add(cid)
        return cid

    # ── widget_list ──────────────────────────────────────────────
    async def list_(
        self,
        *,
        title: str,
        items: list[dict],
        priority: int = 50,
        tone: str = "info",
        card_id: Optional[str] = None,
        skill_id: Optional[str] = None,
    ) -> str:
        """Emit a ranked list widget to the Tab5 home live-slot.

        items: up to 5 dicts shaped {"text": str, "value": str}.  Extra
        items are silently dropped (Tab5 renders top 3 anyway, but the
        store keeps up to 5 for scroll-later).

        Tab5 renders this as a title + numbered rows on the home slot,
        growing the card height to ~168 px.  Competes with widget_live
        on the same priority queue.

        v4·D Phase 4c (TinkerTab widget.h supports type=LIST).
        """
        sid = skill_id or self._skill_id
        cid = card_id or _gen_card_id(sid)
        # Truncate per-item strings to Tab5's widget.h field widths so
        # over-long entries don't get silently cut at the parser.
        safe_items = []
        for it in items[:5]:
            if not isinstance(it, dict):
                continue
            safe_items.append({
                "text":  str(it.get("text",  ""))[:79],
                "value": str(it.get("value", ""))[:15],
            })
        msg: dict = {
            "type": "widget_list",
            "skill_id": sid,
            "card_id": cid,
            "title": title[:63],
            "tone": tone,
            "priority": max(0, min(100, int(priority))),
            "items": safe_items,
        }
        await self._safe_send(msg, describe=f"list {sid}/{cid}")
        self._live_cards.add(cid)
        return cid

    async def live_update(
        self,
        card_id: str,
        *,
        body: Optional[str] = None,
        tone: Optional[str] = None,
        progress: Optional[float] = None,
        action: Optional[Action] = None,
    ) -> None:
        """Partial update to a previously-created live widget."""
        msg: dict = {"type": "widget_live_update", "card_id": card_id}
        if body is not None:
            msg["body"] = body[:255]
        if tone is not None:
            msg["tone"] = tone
        if progress is not None:
            msg["progress"] = float(max(0.0, min(1.0, progress)))
        if action:
            msg["action"] = {"label": action[0][:15], "event": action[1][:47]}
        await self._safe_send(msg, describe=f"live_update {card_id}")

    async def live_clear(self, card_id: Optional[str] = None) -> None:
        """Dismiss a specific live widget, or all owned by this surface."""
        if card_id:
            await self._safe_send(
                {"type": "widget_live_dismiss", "card_id": card_id},
                describe=f"live_dismiss {card_id}",
            )
            self._live_cards.discard(card_id)
        else:
            for cid in list(self._live_cards):
                await self._safe_send(
                    {"type": "widget_live_dismiss", "card_id": cid},
                    describe=f"live_dismiss {cid}",
                )
            self._live_cards.clear()

    # ── widget_card ──────────────────────────────────────────────
    async def card(
        self,
        *,
        title: str,
        body: str,
        tone: str = "info",
        icon: Optional[str] = None,
        image_url: Optional[str] = None,
        action: Optional[Action] = None,
        card_id: Optional[str] = None,
        skill_id: Optional[str] = None,
    ) -> str:
        """Push a card into the activity stream / chat."""
        sid = skill_id or self._skill_id
        cid = card_id or _gen_card_id(sid)
        msg: dict = {
            "type": "widget_card",
            "skill_id": sid,
            "card_id": cid,
            "title": title[:63],
            "body": body[:255],
            "tone": tone,
        }
        if icon:
            msg["icon"] = icon[:15]
        if image_url:
            msg["image_url"] = image_url
        if action:
            msg["action"] = {"label": action[0][:15], "event": action[1][:47]}
        await self._safe_send(msg, describe=f"card {sid}/{cid}")
        return cid

    async def dismiss(self, card_id: str) -> None:
        """Generic dismiss — used for non-live widgets."""
        await self._safe_send(
            {"type": "widget_dismiss", "card_id": card_id},
            describe=f"dismiss {card_id}",
        )

    # ── internal ─────────────────────────────────────────────────
    async def _safe_send(self, msg: dict, *, describe: str) -> None:
        try:
            await self._send(msg)
        except (asyncio.CancelledError, ConnectionError):
            raise
        except Exception as e:  # closed WS / serialization / broken pipe
            log.warning("Tab5Surface send dropped (%s): %s", describe, e)
