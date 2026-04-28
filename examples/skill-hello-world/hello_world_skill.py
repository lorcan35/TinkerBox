"""Hello-world skill — smallest possible widget-emitting skill.

What it does
------------
The user asks "show me a hello widget" or "say hi to me on the
home screen", and this skill emits a single ``widget_live`` to
the Tab5 home screen for 30 seconds.  Demonstrates the full
widget pipeline end-to-end without any real-world functionality.

Concepts shown
--------------
- A skill is implemented as a ``Tool`` subclass that, when
  invoked, emits widget state via the SurfaceManager rather
  than (or in addition to) returning a JSON result.
- Widgets are emitted via ``surface_manager.emit_widget_live``
  / ``emit_widget_dismiss``; the manager owns priority + queue.
- The widget vocabulary (title, body, icon, tone, progress,
  action) is the layout contract — Tab5 renders opinionatedly
  using v4·C theme tokens.  Skills never write layout.

How to use it
-------------
1. Copy this file into ``dragon_voice/tools/hello_world_skill.py``.
2. Register in ``dragon_voice/lifecycle/startup.py`` AFTER the
   SurfaceManager is constructed::

       from dragon_voice.tools.hello_world_skill import HelloWorldSkill
       server._tool_registry.register(HelloWorldSkill(server._surface_mgr))

3. Restart ``tinkerclaw-voice`` and ask "show me a hello widget".

Test it
-------
    curl -s -X POST -H "Authorization: Bearer $DRAGON_API_TOKEN" \\
         -H "Content-Type: application/json" \\
         -d '{"name": "World"}' \\
         http://localhost:3502/api/v1/tools/hello_world_widget/execute

Tab5's home screen should pop a live widget with "Hello, World!"
that fades out after 30 seconds.

For the full reference skill (Pomodoro, with state transitions
and tone gradients across the timer's life), see
``dragon_voice/tools/timesense_tool.py``.

For the widget vocabulary spec, see TinkerTab's
``docs/WIDGETS.md``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from dragon_voice.tools.base import Tool

log = logging.getLogger(__name__)


class HelloWorldSkill(Tool):
    """Emit a 30-second 'Hello, <name>!' widget to the home screen."""

    def __init__(self, surface_mgr) -> None:
        # SurfaceManager is the pluggable layer that owns the
        # widget queue + priority resolution + WS transport
        # to Tab5.  Skills don't talk WS directly; they emit
        # via the surface manager.
        self._surface = surface_mgr
        self._cancel: Optional[asyncio.Task] = None

    @property
    def name(self) -> str:
        return "hello_world_widget"

    @property
    def description(self) -> str:
        return (
            "Show a friendly greeting widget on the user's home screen "
            "for 30 seconds. Use when the user explicitly asks for a "
            "hello widget or a screen demo."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Person to greet.",
                    "default": "World",
                },
            },
        }

    async def execute(self, args: dict) -> dict:
        name = str(args.get("name") or "World")

        # Cancel any prior dismiss task so back-to-back invocations
        # don't race the previous timeout.
        if self._cancel and not self._cancel.done():
            self._cancel.cancel()

        # Emit the live widget.  Card_id is the dismiss handle —
        # Tab5 keeps showing it until we send widget_dismiss.
        card_id = "hello-world-greeting"
        await self._surface.emit_widget_live(
            card_id=card_id,
            title=f"Hello, {name}!",
            body="Welcome to TinkerClaw.",
            icon="wave",        # Tab5 maps to its v4·C wave glyph
            tone="calm",        # green/orange tone family
            priority=10,        # low — yields to time-sensitive widgets
            # No action callback — this is read-only display.
        )

        # Schedule dismissal after 30 seconds.  Use create_task so
        # we return promptly to the LLM with the success result.
        self._cancel = asyncio.create_task(self._auto_dismiss(card_id, 30))

        log.info("hello_world: emitted widget for %r", name)
        return {
            "status": "shown",
            "name": name,
            "card_id": card_id,
            "duration_s": 30,
        }

    async def _auto_dismiss(self, card_id: str, after_s: int) -> None:
        try:
            await asyncio.sleep(after_s)
            await self._surface.emit_widget_dismiss(card_id)
            log.info("hello_world: dismissed %s after %ds", card_id, after_s)
        except asyncio.CancelledError:
            # New invocation arrived; this dismiss is stale, exit
            # quietly without dismissing.
            pass
        except Exception as e:
            log.warning("hello_world dismiss error: %s", e)
