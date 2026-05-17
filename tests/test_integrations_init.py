"""#341 / #348 — `dragon_voice/lifecycle/integrations_init.py`.

Verifies the tool-registration wiring for TinkerBox-native
integrations.  Mirrors the failure-isolation pattern of
`test_notes_db_async.py` — the init step must register the calendar
tools when possible AND swallow import errors at WARNING so a broken
optional integration doesn't take down boot.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from dragon_voice.lifecycle.integrations_init import init_integration_tools


class _FakeRegistry:
    """Records registrations + lets tests assert by tool name."""

    def __init__(self) -> None:
        self.registered: list[object] = []

    def register(self, tool) -> None:  # noqa: ANN001
        self.registered.append(tool)

    @property
    def names(self) -> list[str]:
        return [getattr(t, "name", repr(t)) for t in self.registered]


@pytest.mark.asyncio
async def test_registers_all_calendar_and_gmail_tools_on_clean_registry():
    server = MagicMock()
    server._tool_registry = _FakeRegistry()

    await init_integration_tools(server)

    names = server._tool_registry.names
    # Calendar — 4 tools.
    assert "calendar_today" in names
    assert "calendar_week" in names
    assert "calendar_create" in names
    assert "calendar_cancel" in names
    # Gmail — 5 tools (Phase 2).
    assert "gmail_unread" in names
    assert "gmail_search" in names
    assert "gmail_read" in names
    assert "gmail_send" in names
    assert "gmail_archive" in names


@pytest.mark.asyncio
async def test_gmail_import_failure_does_not_block_calendar(monkeypatch, caplog):
    """If Gmail's tool module fails to import, Calendar tools still
    register — each integration block is independently failure-isolated."""
    monkeypatch.delitem(sys.modules, "dragon_voice.tools.gmail_tool", raising=False)
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def _raising_import(name, *args, **kwargs):
        if name == "dragon_voice.tools.gmail_tool":
            raise ImportError("simulated: gmail tool unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _raising_import)
    server = MagicMock()
    server._tool_registry = _FakeRegistry()
    with caplog.at_level("WARNING"):
        await init_integration_tools(server)

    names = server._tool_registry.names
    assert "calendar_today" in names  # Calendar still landed.
    assert "gmail_unread" not in names
    assert any("Gmail tools not available" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_no_tool_registry_is_silent_noop():
    """If the registry didn't initialize (e.g. agentic init failed) the
    function must return cleanly — boot continues without tools."""
    server = MagicMock()
    server._tool_registry = None
    # Must not raise.
    await init_integration_tools(server)


@pytest.mark.asyncio
async def test_calendar_import_failure_does_not_block_gmail(monkeypatch, caplog):
    """Simulate the google_calendar_tool module being missing.  Boot
    must continue with a single WARNING AND the Gmail tools still
    register — each integration block is independently failure-isolated."""
    monkeypatch.delitem(
        sys.modules, "dragon_voice.tools.google_calendar_tool", raising=False,
    )
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def _raising_import(name, *args, **kwargs):
        if name == "dragon_voice.tools.google_calendar_tool":
            raise ImportError("simulated: calendar tool unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _raising_import)

    server = MagicMock()
    server._tool_registry = _FakeRegistry()
    with caplog.at_level("WARNING"):
        await init_integration_tools(server)

    names = server._tool_registry.names
    assert "calendar_today" not in names
    assert "gmail_unread" in names  # Gmail still landed.
    assert any(
        "Google Calendar tools not available" in rec.message
        for rec in caplog.records
    )
