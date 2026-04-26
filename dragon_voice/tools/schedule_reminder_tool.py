"""ScheduleReminderTool — LLM-facing scheduler tool.

Phase 5 ε1a (refs #126, #128).  See docs/RFC-scheduler.md C.1/C.2
for the schema + return shape.

The tool is dumb: parse `when` via the shared parser, look up the
device_id from the session_id (injected by ConversationEngine —
see conversation.py:223), build a Notification, hand off to
SchedulerManager.  Returns a structured dict the LLM can read to
confirm with the user (`fires_in`, `fires_at_local`).

The conversation engine injects `session_id` into every tool call
(server.py:1130-ish via the on_tool_call closure flow).  The tool
turns that into a `device_id` by looking up the session row.
Without a valid session_id, the tool falls back to `device_id=None`
and the manager handles the broadcast case (currently no-op).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Optional

from dragon_voice.scheduler.manager import (
    RUNAWAY_CAP_PER_DEVICE,
    RunawayCapError,
    SchedulerManager,
)
from dragon_voice.scheduler.models import Notification
from dragon_voice.scheduler.parser import parse_when
from dragon_voice.tools.base import Tool

logger = logging.getLogger(__name__)


# Tone mapping from LLM-facing priority enum → widget_card tone.
# Pin here so a future schema addition (e.g. priority="urgent") is
# a deliberate change in this dict, not silent fall-through.
_PRIORITY_TONE = {
    "normal": "info",
    "important": "warn",
}


class ScheduleReminderTool(Tool):
    """Schedule a reminder for the current user's device.

    LLM-facing arg schema (RFC C.1):
      when     — string.  Relative ("5m"), ISO 8601, or natural ("tomorrow at 3pm").
      message  — string.  The reminder body shown to the user.
      title    — string, optional.  Default "Reminder".
      priority — "normal" | "important", optional.  Default "normal".

    Returns (RFC C.2):
      success → dict with notification_id, fires_at_iso, fires_at_local,
                fires_in (human-readable), title, message
      failure → dict with scheduled=False + error string
    """

    def __init__(self, scheduler_mgr: SchedulerManager, db) -> None:
        self._mgr = scheduler_mgr
        self._db = db

    @property
    def name(self) -> str:
        return "schedule_reminder"

    @property
    def description(self) -> str:
        return (
            "Schedule a reminder to fire later. The user will see a "
            "card in their chat at the specified time."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "when": {
                    "type": "string",
                    "description": (
                        "When to fire the reminder. Accepts: relative "
                        "duration ('5m', '2h30m', '1d'), ISO 8601 "
                        "timestamp ('2026-04-26T15:00:00-04:00'), or a "
                        "natural phrase ('tomorrow at 3pm', 'today at "
                        "17:00'). Bare times ('3pm') resolve in the "
                        "server's local timezone."
                    ),
                },
                "message": {
                    "type": "string",
                    "description": "The reminder text the user will see (1-255 chars).",
                },
                "title": {
                    "type": "string",
                    "description": "Optional short label (default 'Reminder', max 63 chars).",
                },
                "priority": {
                    "type": "string",
                    "enum": ["normal", "important"],
                    "description": "Default 'normal'. 'important' renders with warn tone.",
                },
            },
            "required": ["when", "message"],
        }

    async def execute(self, args: dict) -> dict:
        when_str = (args.get("when") or "").strip()
        message = (args.get("message") or "").strip()
        title = (args.get("title") or "Reminder").strip()
        priority = (args.get("priority") or "normal").strip().lower()

        if not when_str:
            return {"scheduled": False, "error": "'when' is required"}
        if not message:
            return {"scheduled": False, "error": "'message' is required"}

        # Parse `when` against the local clock + Dragon's local TZ.
        # The local TZ is `time.localtime().tm_zone`-derived — but
        # `datetime.now().astimezone()` is the canonical way to get
        # the current local TZ object.  Done once per call (cheap).
        now = time.time()
        local_tz = datetime.now().astimezone().tzinfo

        try:
            fire_at = parse_when(when_str, now=now, tz=local_tz)
        except ValueError as e:
            logger.info(
                "ScheduleReminderTool rejected when=%r: %s", when_str, e,
            )
            return {
                "scheduled": False,
                "error": f"Could not parse 'when': {e}",
            }

        # Resolve session_id → device_id.  ConversationEngine injects
        # session_id; the manager needs device_id for delivery.  If
        # we can't resolve it, fall back to device_id=None and let
        # the manager apply its broadcast-handling rules (currently
        # log + drop at fire time).
        session_id = args.get("session_id") or ""
        device_id = await self._lookup_device_id(session_id)

        notif = Notification(
            device_id=device_id,
            originating_session_id=session_id or None,
            fire_at=fire_at,
            title=title,
            body=message,
            tone=_PRIORITY_TONE.get(priority, "info"),
        )

        try:
            scheduled = await self._mgr.schedule(notif)
        except RunawayCapError as e:
            logger.warning("ScheduleReminderTool runaway cap hit: %s", e)
            return {
                "scheduled": False,
                "error": (
                    f"Too many pending reminders for this device "
                    f"(cap is {RUNAWAY_CAP_PER_DEVICE}).  Cancel some "
                    f"existing reminders first."
                ),
            }

        # Build the human-readable confirmation strings.  These go
        # to the LLM, which will speak them back to the user — so
        # they need to be natural, not raw timestamps.
        fires_at_dt = datetime.fromtimestamp(fire_at, tz=local_tz)
        fires_in_seconds = max(0, fire_at - now)
        return {
            "scheduled": True,
            "notification_id": scheduled.id,
            "fires_at_iso": fires_at_dt.isoformat(),
            "fires_at_local": fires_at_dt.strftime("%-I:%M %p %Z").strip(),
            "fires_in": _humanize_duration(fires_in_seconds),
            "title": scheduled.title,
            "message": scheduled.body,
            "cancel_hint": (
                "User can dismiss the card when it fires; cancel "
                "earlier via the dashboard."
            ),
        }

    async def _lookup_device_id(self, session_id: str) -> Optional[str]:
        """Read device_id off the session row.  Returns None if the
        session is missing — the manager treats that as broadcast."""
        if not session_id:
            return None
        try:
            session = await self._db.get_session(session_id)
            return session.get("device_id") if session else None
        except Exception as e:
            logger.warning("device_id lookup failed for session %s: %s",
                           session_id, e)
            return None


# ───────────────────────── helpers


def _humanize_duration(seconds: float) -> str:
    """Convert a seconds count to a short human-readable string.
    "5 minutes", "2 hours", "1 hour 30 minutes", "3 days".  No
    fractional units — keeps the LLM-spoken output natural."""
    seconds = int(round(seconds))
    if seconds <= 0:
        return "now"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} day" + ("s" if days != 1 else ""))
    if hours:
        parts.append(f"{hours} hour" + ("s" if hours != 1 else ""))
    if minutes:
        parts.append(f"{minutes} minute" + ("s" if minutes != 1 else ""))
    if not parts:
        # Sub-minute: report seconds for clarity in tests + live use
        return f"{secs} second" + ("s" if secs != 1 else "")
    return " ".join(parts)


__all__ = ["ScheduleReminderTool"]
