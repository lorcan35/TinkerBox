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

# Wave 13 H9 (ruff F821 fix): the `prompt(..., on_action=...)` signature
# referenced `ActionHandler` but the import lived only in manager.py, so a
# runtime-evaluated annotation (outside `from __future__ import annotations`
# semantics at class-define time) would have NameError'd.  Define the alias
# locally so the base module is self-contained.
ActionHandler = Callable[[str, dict], Awaitable[Any]]

# Type for the ws-send callable the surface uses. aiohttp WebSocketResponse.send_json
# is async and raises on closed sockets; caller handles that.
SendJson = Callable[[dict], Awaitable[Any]]

Action = Tuple[str, str]  # (label, event)


def _gen_card_id(skill_id: str | None = None) -> str:
    return f"{skill_id or 'widget'}_{uuid.uuid4().hex[:8]}"


class Tab5Surface:
    """Per-session facade. One instance per connected Tab5."""

    def __init__(self, send_json: SendJson, skill_id: str = "unknown",
                 caps: Optional[dict] = None) -> None:
        self._send = send_json
        self._skill_id = skill_id
        # Track live card_ids emitted by this surface so we can clear() later.
        self._live_cards: set[str] = set()
        self._caps: dict = caps or {}
        # Wave 10 B6/K3: bound lazily by SurfaceManager.register_session so
        # surface.prompt(on_action=...) can route handler registrations
        # without the skill referencing the manager directly.
        self._manager = None
        self._session_id: Optional[str] = None

    def for_skill(self, skill_id: str) -> "Tab5Surface":
        """Return a child surface tagged with a specific skill id. The
        underlying send is shared; only the default skill_id differs.

        Wave 8 audit B14/#8 fix: propagate parent caps to the child so
        per-skill surfaces also respect Tab5-declared limits. Previously
        the child got a fresh empty dict and list/prompt/chart helpers
        fell back to hardcoded defaults, making the widget_capabilities
        probe cosmetic for anything emitted through a scoped surface.

        Wave 10 B6/K3 fix: also carry the _manager + _session_id bindings
        so ``surface.for_skill(...).prompt(on_action=handler)`` registers
        the handler on the right session without the skill having to
        touch the manager directly."""
        child = Tab5Surface(self._send, skill_id=skill_id, caps=self._caps)
        child._live_cards = self._live_cards
        child._manager = getattr(self, "_manager", None)
        child._session_id = getattr(self, "_session_id", None)
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
        _max_items = int(self._caps.get("list_max_items", 5) or 5)
        for it in items[:_max_items]:
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

    # ── widget_media ─────────────────────────────────────────────
    async def media(
        self,
        *,
        url: str,
        alt: str = "",
        title: str = "",
        body: str = "",
        tone: str = "info",
        priority: int = 60,
        card_id: Optional[str] = None,
        skill_id: Optional[str] = None,
    ) -> str:
        """Emit a media widget (image + caption) to the Tab5 live-slot.

        Skills shipping photos, screenshots, or chart thumbnails use this
        surface.  `url` should be fetchable from the Tab5 (either Dragon
        /api/media/* or a LAN-reachable origin).  v4·D Phase 4g.
        """
        sid = skill_id or self._skill_id
        cid = card_id or _gen_card_id(sid)
        msg: dict = {
            "type": "widget_media",
            "skill_id": sid,
            "card_id": cid,
            "url": url,
            "tone": tone,
            "priority": max(0, min(100, int(priority))),
        }
        if alt:   msg["alt"]   = alt[:95]
        if title: msg["title"] = title[:63]
        if body:  msg["body"]  = body[:255]
        # Wave 8 audit #8: carry the Tab5-declared media dimensions so the
        # client can reserve layout space without downloading the image.
        # The skill can override via `width`/`height` args in a future
        # pass; for now we default to the client-advertised max so images
        # get a correct aspect-ratio placeholder.
        _max_w = int(self._caps.get("media_max_w", 660) or 660)
        _max_h = int(self._caps.get("media_max_h", 440) or 440)
        msg["width"] = _max_w
        msg["height"] = _max_h
        await self._safe_send(msg, describe=f"media {sid}/{cid}")
        self._live_cards.add(cid)
        return cid

    # ── widget_prompt ────────────────────────────────────────────
    async def prompt(
        self,
        *,
        title: str,
        choices: list[tuple],
        body: str = "",
        tone: str = "active",
        priority: int = 70,
        card_id: Optional[str] = None,
        skill_id: Optional[str] = None,
        on_action: Optional[ActionHandler] = None,
    ) -> str:
        """Emit a prompt widget (title + up to 3 button choices).

        `choices` is a list of (text, event) tuples. Tab5 renders each
        as a row; tapping fires widget_action carrying the matching
        event.

        Wave 10 audit B6/K3 — declarative action dispatch:
        Pass ``on_action=handler`` and the Surface auto-registers the
        callback with the SurfaceManager for this card_id. Skills no
        longer have to call ``register_action`` imperatively AFTER the
        emit, which was the source of the "platform works for any skill"
        overclaim (half the skills forgot the second call). When
        ``on_action`` is None the caller can still register manually
        via ``SurfaceManager.register_action`` — useful for multi-card
        workflows where one handler services several prompts.
        """
        sid = skill_id or self._skill_id
        cid = card_id or _gen_card_id(sid)
        safe = []
        _max_choices = int(self._caps.get("prompt_max_choices", 3) or 3)
        for c in choices[:_max_choices]:
            if not isinstance(c, (list, tuple)) or len(c) < 2:
                continue
            txt, ev = c[0], c[1]
            safe.append({
                "text":  str(txt)[:47],
                "event": str(ev)[:47],
            })
        # Register the handler BEFORE the emit so a fast user tap can
        # never land before the manager knows who owns the card.
        if on_action is not None:
            mgr = getattr(self, "_manager", None)
            session_id = getattr(self, "_session_id", None)
            if mgr is not None and session_id is not None:
                mgr.register_action(session_id, cid, on_action)
            else:
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "surface.prompt(on_action=...) dropped — surface has "
                    "no manager/session binding. Use SurfaceManager."
                    "register_action(sid, cid, handler) instead."
                )
        msg: dict = {
            "type": "widget_prompt",
            "skill_id": sid,
            "card_id": cid,
            "title": title[:63],
            "tone": tone,
            "priority": max(0, min(100, int(priority))),
            "choices": safe,
        }
        if body: msg["body"] = body[:255]
        await self._safe_send(msg, describe=f"prompt {sid}/{cid}")
        self._live_cards.add(cid)
        return cid

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

    # ── widget_chart ─────────────────────────────────────────────
    async def chart(
        self,
        *,
        title: str,
        values: list[float],
        body: str = "",
        tone: str = "info",
        chart_max: float = 0.0,
        priority: int = 60,
        card_id: Optional[str] = None,
        skill_id: Optional[str] = None,
    ) -> str:
        """Emit a bar/line chart widget (up to 12 points).

        Audit B5/B13 (2026-04-20): the chart emitter was missing from
        Tab5Surface, so no skill could ever produce a widget_chart — the
        parser existed on Tab5 with no upstream source. values[] is sent
        as-is; Tab5 normalizes against chart_max for bar heights (0 =
        auto-scale to max of values).
        """
        sid = skill_id or self._skill_id
        cid = card_id or _gen_card_id(sid)
        _max_pts = int(self._caps.get("chart_max_points", 12) or 12)
        pts = [float(v) for v in values[:_max_pts]]
        msg: dict = {
            "type": "widget_chart",
            "skill_id": sid,
            "card_id": cid,
            "title": title[:63],
            "tone": tone,
            "priority": max(0, min(100, int(priority))),
            "values": pts,
            "max": float(chart_max),
        }
        if body:
            msg["body"] = body[:255]
        await self._safe_send(msg, describe=f"chart {sid}/{cid}")
        self._live_cards.add(cid)
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
