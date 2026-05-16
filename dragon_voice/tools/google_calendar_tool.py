"""Voice-callable tools for Google Calendar (#341 / #342).

Thin `Tool` wrappers around `GoogleCalendarIntegration` so the LLM in
any vmode can call `calendar_today`, `calendar_week`, `calendar_create`,
`calendar_cancel` via the existing ToolRegistry mechanism.

Return shape mirrors the rest of `dragon_voice/tools/` — plain dicts
that the LLM injects back into context.  `error` key absent = success.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from dragon_voice.tools.base import Tool
from dragon_voice.tools.integrations.google.calendar import (
    GoogleCalendarIntegration,
)
from dragon_voice.tools.integrations.oauth import DeviceCodeError

logger = logging.getLogger(__name__)


_shared_instance: Optional[GoogleCalendarIntegration] = None


def _shared_integration() -> GoogleCalendarIntegration:
    """Process-wide singleton so token cache + cred-store load are
    amortized.  Concurrency safe: each `_authed_*` call opens its own
    aiohttp session."""
    global _shared_instance
    if _shared_instance is None:
        _shared_instance = GoogleCalendarIntegration()
    return _shared_instance


def _not_connected(action: str) -> dict[str, Any]:
    return {
        "error": "not_connected",
        "message": (
            f"Google Calendar isn't connected yet, so I can't {action}.  "
            "Tap Settings → Integrations → Connect Google on the Tab5 to "
            "set it up."
        ),
    }


class CalendarTodayTool(Tool):
    """List today's events."""

    priority = 30  # surface in compact prompt for local models

    @property
    def name(self) -> str:
        return "calendar_today"

    @property
    def description(self) -> str:
        return (
            "List today's events from the user's primary Google Calendar.  "
            "Use when the user asks 'what's on my calendar today' or 'what "
            "do I have going on today'."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Max events to return (default 10).",
                },
            },
        }

    async def execute(self, args: dict) -> dict:
        integ = _shared_integration()
        if not await integ.is_connected():
            return _not_connected("read your calendar")
        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        try:
            events = await integ.list_events(
                time_min=start, time_max=end,
                max_results=int(args.get("max_results", 10)),
            )
        except DeviceCodeError as e:
            return {"error": e.code, "message": e.description}
        return {
            "count": len(events),
            "events": events,
        }


class CalendarWeekTool(Tool):
    """List events for the next 7 days."""

    @property
    def name(self) -> str:
        return "calendar_week"

    @property
    def description(self) -> str:
        return (
            "List events for the next 7 days from the user's primary Google "
            "Calendar.  Use when the user asks 'what's on this week' or "
            "'what's coming up'."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Max events to return (default 20).",
                },
            },
        }

    async def execute(self, args: dict) -> dict:
        integ = _shared_integration()
        if not await integ.is_connected():
            return _not_connected("read your calendar")
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=7)
        try:
            events = await integ.list_events(
                time_min=now, time_max=end,
                max_results=int(args.get("max_results", 20)),
            )
        except DeviceCodeError as e:
            return {"error": e.code, "message": e.description}
        return {"count": len(events), "events": events}


class CalendarCreateTool(Tool):
    """Create a calendar event.  Agent should confirm with user first."""

    @property
    def name(self) -> str:
        return "calendar_create"

    @property
    def description(self) -> str:
        return (
            "Create an event on the user's primary Google Calendar.  Use "
            "when the user explicitly asks to schedule / add / book a "
            "calendar event.  Confirm details with the user BEFORE calling; "
            "calendar writes are not undoable by voice except via "
            "calendar_cancel."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "required": ["summary", "start_iso", "end_iso"],
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Event title (e.g. 'Dentist appointment').",
                },
                "start_iso": {
                    "type": "string",
                    "description": "Start as ISO 8601 with timezone (e.g. '2026-05-17T14:00:00+02:00').",
                },
                "end_iso": {
                    "type": "string",
                    "description": "End as ISO 8601 with timezone.",
                },
                "location": {
                    "type": "string",
                    "description": "Optional venue or address.",
                },
                "description": {
                    "type": "string",
                    "description": "Optional longer description.",
                },
            },
        }

    async def execute(self, args: dict) -> dict:
        integ = _shared_integration()
        if not await integ.is_connected():
            return _not_connected("create an event")
        try:
            start = datetime.fromisoformat(args["start_iso"])
            end = datetime.fromisoformat(args["end_iso"])
        except (ValueError, KeyError) as e:
            return {"error": "bad_args", "message": str(e)}
        try:
            ev = await integ.create_event(
                summary=args["summary"],
                start=start,
                end=end,
                location=args.get("location"),
                description=args.get("description"),
            )
        except DeviceCodeError as e:
            return {"error": e.code, "message": e.description}
        return {"event": ev, "created": True}


class CalendarCancelTool(Tool):
    """Delete an event by id."""

    @property
    def name(self) -> str:
        return "calendar_cancel"

    @property
    def description(self) -> str:
        return (
            "Delete an event from the user's primary Google Calendar.  Use "
            "when the user explicitly asks to cancel / delete / remove an "
            "event.  Confirm the event id (from a previous calendar_today "
            "or calendar_week result) with the user before calling."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "required": ["event_id"],
            "properties": {
                "event_id": {
                    "type": "string",
                    "description": "Event id from a previous calendar_today or calendar_week result.",
                },
            },
        }

    async def execute(self, args: dict) -> dict:
        integ = _shared_integration()
        if not await integ.is_connected():
            return _not_connected("cancel an event")
        ok = await integ.cancel_event(args["event_id"])
        if not ok:
            return {"error": "cancel_failed", "event_id": args["event_id"]}
        return {"cancelled": True, "event_id": args["event_id"]}
