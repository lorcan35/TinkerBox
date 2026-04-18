"""Time Sense — AI-first timer. First reference skill for the Widget Platform.

Exercises every slot of widget_live:
  - title, body, icon, tone, progress, action, priority
  - state transitions calm → active → approaching → urgent → done
  - action callback (pause/resume)
  - completion card

See TinkerTab/docs/WIDGETS.md §3.1 for the tone rendering contract.
"""
from __future__ import annotations
import asyncio
import logging
import re
from typing import Optional

from dragon_voice.tools.base import Tool

log = logging.getLogger(__name__)


def _fmt_remaining(sec: int) -> str:
    if sec < 0:
        sec = 0
    m, s = divmod(sec, 60)
    return f"{m:02d}:{s:02d} remaining"


def _tone_for(progress: float) -> str:
    """Progress → tone mapping. Matches the 5-state narrative in the
    Time Sense mockup (01-time-sense-flow.html)."""
    if progress >= 0.97:
        return "urgent"       # last ~1 min
    if progress >= 0.85:
        return "approaching"  # ~last 15%
    return "active"


class TimesenseTimer:
    """Stateful per-session timer. Created per /timer invocation."""

    def __init__(self, surface, manager, session_id: str, minutes: int) -> None:
        self.surface = surface
        self.manager = manager
        self.session_id = session_id
        self.total_sec = max(60, int(minutes) * 60)
        self.remaining = self.total_sec
        self.paused = False
        self.card_id: Optional[str] = None
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> str:
        minutes = self.total_sec // 60
        self.card_id = await self.surface.live(
            title="Deep work",
            body=_fmt_remaining(self.total_sec),
            icon="briefcase",
            tone="calm",
            progress=0.0,
            action=("PAUSE", "ts.pause"),
            priority=80,
            skill_id="timesense.pomodoro",
            card_id=f"ts_{self.session_id[:8]}",
        )
        self.manager.register_action(self.session_id, self.card_id, self._on_action)
        self._task = asyncio.create_task(self._run())
        log.info("TimeSense start: %d min (session=%s card=%s)",
                 minutes, self.session_id, self.card_id)
        return self.card_id

    async def _run(self) -> None:
        try:
            while self.remaining > 0:
                await asyncio.sleep(1)
                if self.paused:
                    continue
                self.remaining -= 1
                pct = 1 - (self.remaining / self.total_sec)
                tone = _tone_for(pct)
                await self.surface.live_update(
                    self.card_id,
                    body=_fmt_remaining(self.remaining),
                    tone=tone,
                    progress=pct,
                )
            await self._finish()
        except asyncio.CancelledError:
            await self._cleanup()
            raise

    async def _finish(self) -> None:
        total_min = self.total_sec // 60
        # 1. Settle the orb into "done" tone briefly
        await self.surface.live_update(
            self.card_id,
            body=f"Well done. {total_min} min of focus.",
            tone="done",
            progress=1.0,
        )
        # 2. Emit a completion card to the chat stream
        await self.surface.card(
            title="Timer done",
            body=f"{total_min} min · well run",
            tone="success",
            icon="check",
            skill_id="timesense.pomodoro",
        )
        # 3. Clear the live slot after a short pause so the user sees "done"
        await asyncio.sleep(6)
        await self._cleanup()

    async def _cleanup(self) -> None:
        if self.card_id:
            await self.surface.live_clear(self.card_id)
            self.manager.unregister_action(self.session_id, self.card_id)

    async def _on_action(self, event: str, payload: dict) -> None:
        if event == "ts.pause":
            self.paused = not self.paused
            tone = "approaching" if self.paused else _tone_for(1 - self.remaining / self.total_sec)
            body = (
                f"Paused at {_fmt_remaining(self.remaining)}"
                if self.paused
                else _fmt_remaining(self.remaining)
            )
            await self.surface.live_update(
                self.card_id,
                body=body,
                tone=tone,
                action=("RESUME", "ts.pause") if self.paused else ("PAUSE", "ts.pause"),
            )
            log.info("TimeSense %s (card=%s)", "paused" if self.paused else "resumed", self.card_id)
        elif event == "ts.stop":
            if self._task:
                self._task.cancel()


# Registry of active timers so the tool can address a running one.
_ACTIVE: dict[str, TimesenseTimer] = {}


class TimesenseTool(Tool):
    """LLM-exposed tool: 'Set a Pomodoro / focus timer.'"""

    def __init__(self, surface_manager) -> None:
        self._mgr = surface_manager

    @property
    def name(self) -> str:
        return "timesense_timer"

    @property
    def description(self) -> str:
        return (
            "Start an AI-first focus timer. The orb becomes the timer — "
            "color shifts from calm to urgent as time runs low, narrative "
            "line reflects the state. Default: 25 minutes (Pomodoro)."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "minutes": {
                    "type": "number",
                    "description": "Duration in minutes (default 25, min 1, max 120).",
                },
                "session_id": {
                    "type": "string",
                    "description": "Internal — session id of the requesting device.",
                },
            },
            "required": [],
        }

    @staticmethod
    def parse_voice(transcript: str) -> Optional[int]:
        """Extract minutes from a transcript like 'set a timer for 25 minutes'
        or 'pomodoro' (→ 25 min default)."""
        t = transcript.lower().strip()
        if not t:
            return None
        if "pomodoro" in t or "deep work" in t:
            m = re.search(r"(\d{1,3})\s*(?:min|minute|m\b)", t)
            return int(m.group(1)) if m else 25
        m = re.search(r"(\d{1,3})\s*(?:min|minute|m\b)", t)
        if m and ("timer" in t or "time" in t or "focus" in t):
            return int(m.group(1))
        return None

    async def execute(self, args: dict) -> dict:
        minutes = int(args.get("minutes", 25))
        minutes = max(1, min(120, minutes))
        session_id = args.get("session_id", "unknown")

        surface = self._mgr.surface_for(session_id, "timesense.pomodoro")
        if not surface:
            return {"error": "no surface for session (device not connected?)"}

        # Cancel any existing timer for this session
        if session_id in _ACTIVE:
            await _ACTIVE[session_id]._cleanup()
            old = _ACTIVE.pop(session_id)
            if old._task:
                old._task.cancel()

        timer = TimesenseTimer(surface, self._mgr, session_id, minutes)
        _ACTIVE[session_id] = timer
        card_id = await timer.start()

        return {
            "started": True,
            "minutes": minutes,
            "card_id": card_id,
        }
