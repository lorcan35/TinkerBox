"""SurfaceManager — per-session ownership of Tab5 surfaces + action routing.

On WebSocket connect, a session registers a send callable with the
manager; skills retrieve their Tab5Surface via `manager.surface_for(session_id, skill_id)`.

When Tab5 emits `widget_action`, `manager.handle_action(session_id, event, card_id, payload)`
dispatches to the registered handler for that card_id (if any).
"""
from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional
from typing import Any, Awaitable, Callable, Dict, Optional

from .base import Tab5Surface, SendJson

log = logging.getLogger("tab5.surface_mgr")

ActionHandler = Callable[[str, dict], Awaitable[Any]]
# Action handler: (event, payload) -> awaitable. Payload may be {}.


@dataclass
class _SessionState:
    send: SendJson
    surface: Tab5Surface
    action_handlers: Dict[str, ActionHandler] = field(default_factory=dict)
    # card_id -> handler. Skills register their own card_ids.


class SurfaceManager:
    """Singleton-style. Held by the voice server for the lifetime of the process."""

    def __init__(self) -> None:
        self._sessions: Dict[str, _SessionState] = {}
        self._lock = asyncio.Lock()

    async def register_session(self, session_id: str, send: SendJson,
                                  caps: Optional[dict] = None) -> Tab5Surface:
        """Called when a Tab5 WS connects. Returns the session's default surface."""
        async with self._lock:
            # Wave 10 B6/K3 — bind manager + session_id onto the surface so
            # surface.prompt(on_action=handler) can register via the manager
            # without the skill having to thread references manually.
            surface = Tab5Surface(send, skill_id="session", caps=caps)
            surface._manager = self
            surface._session_id = session_id
            self._sessions[session_id] = _SessionState(send=send, surface=surface)
            log.info("surface registered: session=%s", session_id)
            return surface

    async def unregister_session(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)
            log.info("surface unregistered: session=%s", session_id)

    def surface_for(self, session_id: str, skill_id: str) -> Optional[Tab5Surface]:
        """Get (or create) a surface tagged with the skill's id."""
        state = self._sessions.get(session_id)
        if not state:
            return None
        return state.surface.for_skill(skill_id)

    def register_action(
        self, session_id: str, card_id: str, handler: ActionHandler
    ) -> None:
        state = self._sessions.get(session_id)
        if not state:
            return
        state.action_handlers[card_id] = handler

    def unregister_action(self, session_id: str, card_id: str) -> None:
        state = self._sessions.get(session_id)
        if not state:
            return
        state.action_handlers.pop(card_id, None)

    async def handle_action(
        self,
        session_id: str,
        card_id: str,
        event: str,
        payload: Optional[dict] = None,
    ) -> None:
        """Route incoming widget_action to the owning skill's handler.

        Wave 8 audit B6/K3 fix: when no handler is registered for a card
        we now dismiss the card so the user's tap has an observable
        effect (previously it was a silent log, making the platform look
        broken for any skill that forgot to call register_action).
        TimeSense and the /debug/widget_prompt endpoint register handlers
        explicitly and take this path; drive-by skills get the default
        dismiss below so taps never feel dead.
        """
        state = self._sessions.get(session_id)
        if not state:
            log.warning("widget_action for unknown session=%s", session_id)
            return
        handler = state.action_handlers.get(card_id)
        if not handler:
            log.info("widget_action no handler: card=%s event=%s — "
                     "dispatching default dismiss (B6/K3)", card_id, event)
            try:
                await state.surface.dismiss(card_id)
            except Exception:
                log.exception("widget_action default dismiss failed for card=%s", card_id)
            return
        try:
            await handler(event, payload or {})
        except Exception:
            log.exception("widget_action handler raised: card=%s event=%s", card_id, event)
