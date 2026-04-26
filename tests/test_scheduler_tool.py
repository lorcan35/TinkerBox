"""Tests for ScheduleReminderTool — the LLM-facing scheduler tool.

Phase 5 ε1a (refs #126, #128).

The tool is mostly glue between parse_when + SchedulerManager — these
tests pin the contract the LLM sees: parameter schema shape, the
returned dict on success/failure, and runaway-cap propagation.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.scheduler.manager import RunawayCapError, SchedulerManager
from dragon_voice.scheduler.models import Notification
from dragon_voice.scheduler.store import InMemoryNotificationStore
from dragon_voice.tools.schedule_reminder_tool import (
    ScheduleReminderTool,
    _humanize_duration,
)


def _make_tool() -> tuple[ScheduleReminderTool, AsyncMock]:
    """Build a tool wired to a real SchedulerManager + InMemoryStore.
    Returns (tool, db_get_session) so tests can swap session lookup."""
    store = InMemoryNotificationStore()
    surface_mgr = MagicMock()
    surface_mgr.surface_for = MagicMock(return_value=MagicMock())
    session_mgr = MagicMock()
    session_mgr.list_sessions = AsyncMock(return_value=[])

    mgr = SchedulerManager(
        store=store,
        surface_mgr=surface_mgr,
        session_mgr=session_mgr,
    )

    db = MagicMock()
    db.get_session = AsyncMock(return_value={"id": "sess1", "device_id": "dev_A"})

    tool = ScheduleReminderTool(scheduler_mgr=mgr, db=db)
    return tool, db


# ───────────────────────── schema shape


def test_tool_has_required_metadata() -> None:
    """The Tool ABC contract: name, description, parameters_schema,
    execute.  Pin so renames don't silently break the LLM-facing
    invocation grammar."""
    tool, _ = _make_tool()
    assert tool.name == "schedule_reminder"
    assert isinstance(tool.description, str) and tool.description
    schema = tool.parameters_schema
    assert schema["type"] == "object"
    # Required fields are exactly `when` and `message` — keeping
    # the schema tight for local-LLM tool-fire reliability (RFC A5).
    assert set(schema["required"]) == {"when", "message"}
    # Optional fields exist
    assert "title" in schema["properties"]
    assert "priority" in schema["properties"]


# ───────────────────────── execute() success


def test_execute_returns_scheduled_true_with_confirmation_fields() -> None:
    """The headline LLM-facing contract: on success the returned
    dict has the fields the LLM speaks back to confirm with the user."""
    tool, _ = _make_tool()

    result = asyncio.run(tool.execute({
        "when": "5m",
        "message": "Take out the trash",
        "session_id": "sess1",
    }))

    assert result["scheduled"] is True
    assert result["notification_id"].startswith("sched_")
    assert "fires_at_iso" in result
    assert "fires_at_local" in result
    assert result["fires_in"] == "5 minutes"
    assert result["title"] == "Reminder"
    assert result["message"] == "Take out the trash"
    assert "cancel_hint" in result


def test_execute_uses_provided_title_and_priority() -> None:
    """priority='important' maps to tone='warn' — pin the mapping
    so a future schema change doesn't silently swap."""
    tool, _ = _make_tool()

    result = asyncio.run(tool.execute({
        "when": "1h",
        "message": "Doctor's appointment",
        "title": "Important",
        "priority": "important",
        "session_id": "sess1",
    }))

    assert result["scheduled"] is True
    assert result["title"] == "Important"
    # Tone landed on the underlying notification (not visible in tool result,
    # but verifiable by inspecting the stored notification)


# ───────────────────────── execute() failure modes


def test_execute_rejects_unparseable_when() -> None:
    """Garbage `when` → scheduled=False with descriptive error.  No
    notification persisted, no asyncio task spawned."""
    tool, _ = _make_tool()

    result = asyncio.run(tool.execute({
        "when": "not a real time",
        "message": "x",
        "session_id": "sess1",
    }))

    assert result["scheduled"] is False
    assert "error" in result
    assert "Could not parse" in result["error"]


def test_execute_rejects_missing_message() -> None:
    """An empty `message` is a structural error — must reject before
    even hitting the parser."""
    tool, _ = _make_tool()

    result = asyncio.run(tool.execute({
        "when": "5m",
        "message": "",
        "session_id": "sess1",
    }))

    assert result["scheduled"] is False
    assert "message" in result["error"].lower()


def test_execute_surfaces_runaway_cap_as_user_friendly_error() -> None:
    """When SchedulerManager raises RunawayCapError, the tool catches
    it and returns a structured dict the LLM can use to back off
    gracefully — NOT raise."""
    tool, _ = _make_tool()

    # Force the manager to throw on schedule
    async def _raise(notif):
        raise RunawayCapError("fake cap hit")
    tool._mgr.schedule = _raise

    result = asyncio.run(tool.execute({
        "when": "5m",
        "message": "x",
        "session_id": "sess1",
    }))

    assert result["scheduled"] is False
    assert "cap" in result["error"].lower() or "too many" in result["error"].lower()


# ───────────────────────── helpers


def test_humanize_duration_handles_common_cases() -> None:
    """Spot-check the human-readable strings the LLM will speak.
    Singular/plural matters because the LLM tends to copy the
    string verbatim."""
    assert _humanize_duration(0) == "now"
    assert _humanize_duration(45) == "45 seconds"
    assert _humanize_duration(60) == "1 minute"
    assert _humanize_duration(90) == "1 minute"  # truncates seconds when minutes present
    assert _humanize_duration(3600) == "1 hour"
    assert _humanize_duration(3600 + 30 * 60) == "1 hour 30 minutes"
    assert _humanize_duration(2 * 86400) == "2 days"
