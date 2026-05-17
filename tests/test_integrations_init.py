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
async def test_registers_all_four_calendar_tools_on_clean_registry():
    server = MagicMock()
    server._tool_registry = _FakeRegistry()

    await init_integration_tools(server)

    names = server._tool_registry.names
    assert "calendar_today" in names
    assert "calendar_week" in names
    assert "calendar_create" in names
    assert "calendar_cancel" in names


@pytest.mark.asyncio
async def test_no_tool_registry_is_silent_noop():
    """If the registry didn't initialize (e.g. agentic init failed) the
    function must return cleanly — boot continues without tools."""
    server = MagicMock()
    server._tool_registry = None
    # Must not raise.
    await init_integration_tools(server)


@pytest.mark.asyncio
async def test_calendar_module_import_failure_is_swallowed(monkeypatch, caplog):
    """Simulate the google_calendar_tool module being missing — boot
    must continue, with a single WARNING.  Confirms the try/except
    failure-isolation invariant lives at the right boundary."""
    # Drop the module from cache so the next import sees our stub.
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

    # No tools registered, no exception escaped.
    assert server._tool_registry.registered == []
    assert any(
        "Google Calendar tools not available" in rec.message
        for rec in caplog.records
    )
