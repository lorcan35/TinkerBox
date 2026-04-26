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
    # Audit B1 (#165): per-session turn-gate.  `turn_busy` is True
    # while a voice or text turn is mid-LLM/TTS — out-of-band emits
    # (scheduler reminder fires) defer until the turn completes so
    # they don't interleave between `llm` token frames.
    turn_busy: bool = False
    # Each entry is a zero-arg async callable that performs the
    # deferred send.  Stored as callables (not raw msg + send) so the
    # eventual send-time guard (e.g. `if ws.closed: skip`) lives at
    # the call site, not here.
    deferred_emits: list = field(default_factory=list)


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
            # Audit B1 (#165): pop drops the deferred-emits queue too —
            # no point trying to deliver them after the session is
            # gone.  Their callers' send_fn would no-op on closed WS
            # anyway, but we save the wasted attempts.
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

    # ── Audit B1 (#165): TurnGate — defer out-of-band emits while a
    #    voice/text turn is mid-LLM, drain on turn end, discard on
    #    cancel.  Used by SchedulerManager so reminder fires don't
    #    interleave between LLM token frames.

    def mark_turn_start(self, session_id: str) -> None:
        """Mark this session as mid-turn — out-of-band emits will be
        deferred until `mark_turn_end` (or dropped via `discard_deferred`).

        Idempotent — safe to call when already busy.  No-op for
        unknown sessions (the session may have unregistered between
        the caller's check and now).
        """
        state = self._sessions.get(session_id)
        if state is None:
            return
        state.turn_busy = True

    async def mark_turn_end(self, session_id: str) -> None:
        """Clear the turn-busy flag and drain any deferred emits in
        FIFO order.  Each deferred callable is awaited; an exception
        in one does not block the rest (logged + skipped).

        Safe to call when no turn was in flight (no-op).
        """
        state = self._sessions.get(session_id)
        if state is None:
            return
        state.turn_busy = False
        if not state.deferred_emits:
            return
        pending = state.deferred_emits
        state.deferred_emits = []
        for fn in pending:
            try:
                await fn()
            except Exception:
                log.exception(
                    "deferred emit raised on turn-end drain for session=%s",
                    session_id,
                )

    def discard_deferred(self, session_id: str) -> int:
        """Drop all deferred emits without sending.  Returns the count
        dropped.  Used by the WS cancel handler so a user-initiated
        stop also suppresses any reminder that fired during the turn.
        Does NOT clear the turn_busy flag — caller still needs to
        eventually call `mark_turn_end` (or the surface
        unregister hook).
        """
        state = self._sessions.get(session_id)
        if state is None or not state.deferred_emits:
            return 0
        n = len(state.deferred_emits)
        state.deferred_emits.clear()
        return n

    def is_turn_busy(self, session_id: str) -> bool:
        """True if a voice/text turn is currently in flight for this
        session.  Out-of-band emitters use this to decide whether to
        send immediately or defer."""
        state = self._sessions.get(session_id)
        return bool(state and state.turn_busy)

    async def defer_or_send(
        self,
        session_id: str,
        send_fn: Callable[[], Awaitable[Any]],
    ) -> bool:
        """Either run `send_fn()` now (turn idle / unknown session) or
        queue it for the next `mark_turn_end` drain.

        Returns True if the emit ran immediately, False if it was
        deferred.  Unknown-session always returns True (and runs the
        send_fn) — caller's send_fn is expected to be defensive
        (e.g. check ws.closed) so a vanished session won't raise.
        """
        state = self._sessions.get(session_id)
        if state is None or not state.turn_busy:
            await send_fn()
            return True
        state.deferred_emits.append(send_fn)
        log.debug(
            "deferred out-of-band emit for session=%s (queue=%d)",
            session_id, len(state.deferred_emits),
        )
        return False

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
