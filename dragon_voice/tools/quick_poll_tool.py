"""Quick Poll — reference skill demonstrating the wave 8 declarative
``surface.prompt(on_action=handler)`` pattern.

This is the minimal viable example for a 3rd-party widget skill
(docs/SKILL_AUTHORING.md walks through every step). It's deliberately
simpler than Time Sense:

  - no persistent state across turns
  - no live slot, just a prompt bubble
  - handler is a closure bound at emit time, so no bookkeeping in the
    skill itself

Voice: "ask me a quick poll", "poll me", or any text containing the
word ``poll``. Returns after the user taps a choice — or after a 60 s
timeout with a default answer.

Companion doc: docs/SKILL_AUTHORING.md (wave 12).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from dragon_voice.tools.base import Tool

log = logging.getLogger(__name__)


class QuickPollTool(Tool):
    """Ask the user a 3-choice poll and return whichever they tap.

    Registration::

        from dragon_voice.tools.quick_poll_tool import QuickPollTool
        self._tool_registry.register(QuickPollTool(self._surface_mgr))

    Voice flow::

        User:  "quick poll: coffee or tea?"
        Tool:  emits widget_prompt ["Coffee", "Tea", "Either"]
        User:  taps "Tea"
        Tool:  returns {"answer": "Tea"}
        LLM :  continues the turn with the poll result
    """

    def __init__(self, surface_manager) -> None:
        self._mgr = surface_manager

    # ---------- Tool metadata ----------
    @property
    def name(self) -> str:
        return "quick_poll"

    @property
    def description(self) -> str:
        return (
            "Ask the user a quick poll on the Tab5 screen and wait for "
            "the tap. Use when a conversational choice would benefit from "
            "a visual 2-3 button prompt (Yes/No, "
            "mode selection, preference).  Do NOT use for free-form text — "
            "ask a plain follow-up question instead."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Short prompt title (max 60 chars). "
                                   "Example: 'Coffee or tea?'",
                },
                "choices": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "2-3 choice labels. Tab5 renders each "
                                   "as a row. Example: ['Coffee', 'Tea', 'Either']",
                    "minItems": 2,
                    "maxItems": 3,
                },
                "timeout_s": {
                    "type": "number",
                    "description": "Seconds before we give up and return a default "
                                   "(max 120, default 60).",
                },
            },
            "required": ["question", "choices"],
        }

    # ---------- Execute ----------
    async def execute(self, args: dict) -> dict:
        question = str(args.get("question", "")).strip()
        choices = [str(c) for c in (args.get("choices") or []) if str(c).strip()]
        timeout_s = min(120.0, max(5.0, float(args.get("timeout_s", 60))))
        # Conversation engine injects session_id into args (wave 12) —
        # skills read it from there rather than relying on a kwarg.
        session_id = str(args.get("session_id", "unknown"))

        if not question:
            return {"error": "question is required"}
        if len(choices) < 2:
            return {"error": "at least 2 choices required"}
        choices = choices[:3]  # Surface caps anyway — but give a friendly message

        # Surface bound to this session. for_skill scopes our skill_id onto
        # the card_id + any outgoing widget_action events so the Tab5 side
        # logs are attributable to us.
        surface = self._mgr.surface_for(session_id, "quick_poll")
        if surface is None:
            return {"error": "no Tab5 surface for this session — skill unavailable"}

        # Wave 8 declarative pattern: the handler is a closure that
        # resolves a Future. When the user taps, the manager invokes the
        # handler with the event string; we store it and signal the wait.
        # No need to call register_action separately — surface.prompt(
        # on_action=...) wires it for us using the session bound by the
        # SurfaceManager at register_session time.
        picked: dict = {}
        done = asyncio.Event()

        async def _on_tap(event: str, payload: dict) -> None:
            picked["answer"] = event
            done.set()

        card_id = await surface.prompt(
            title=question[:60],
            choices=[(label, f"poll.{label.lower().replace(' ', '_')}")
                     for label in choices],
            priority=75,  # Above ambient widgets but below TimeSense (80)
            on_action=_on_tap,
        )

        # Wait for the tap OR timeout.  On timeout we still return a
        # reasonable result (first choice) so the LLM has something to
        # continue with — skills should never stall the turn forever.
        try:
            await asyncio.wait_for(done.wait(), timeout=timeout_s)
            answer = picked.get("answer", "").replace("poll.", "")
            return {"answer": answer, "timed_out": False}
        except asyncio.TimeoutError:
            log.info("quick_poll %s: timeout — defaulting to first choice", card_id)
            return {"answer": choices[0], "timed_out": True}
        finally:
            # Dismiss the card (handler is auto-dismissed by the default-
            # dismiss guard in SurfaceManager.handle_action anyway, but
            # being explicit is clearer for reference code).
            try:
                await surface.dismiss(card_id)
            except Exception:
                pass
