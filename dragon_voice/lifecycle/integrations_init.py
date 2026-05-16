"""Integrations layer + LLM-callable tools (#341 / #342 / Phase 2).

Registers tool wrappers for TinkerBox-native integrations on the
existing ``_tool_registry``.  Mirrors ``notes_init.py``'s shape:
optional dep, layered try/except so a missing integration package
doesn't block boot.

Today: Google Calendar + Gmail.  Home Assistant / Spotify / Notion
drop in here as they land.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def init_integration_tools(server: Any) -> None:
    """Register LLM-callable integration tools on ``server._tool_registry``.

    Run AFTER ``init_agentic_modules`` (depends on the registry).
    The integration *backend* itself is instantiated lazily by each
    tool via ``_shared_integration()`` so REST connect/disconnect
    routes and tool calls share a single in-process instance.

    Failure isolation: each integration block is wrapped independently
    so a broken Google import doesn't block Home Assistant tools.
    """
    if not server._tool_registry:
        return

    try:
        from dragon_voice.tools.google_calendar_tool import (
            CalendarCancelTool,
            CalendarCreateTool,
            CalendarTodayTool,
            CalendarWeekTool,
        )
        server._tool_registry.register(CalendarTodayTool())
        server._tool_registry.register(CalendarWeekTool())
        server._tool_registry.register(CalendarCreateTool())
        server._tool_registry.register(CalendarCancelTool())
        logger.info("Google Calendar tools registered (4)")
    except Exception as e:
        logger.warning("Google Calendar tools not available: %s", e)

    try:
        from dragon_voice.tools.gmail_tool import (
            GmailArchiveTool,
            GmailReadTool,
            GmailSearchTool,
            GmailSendTool,
            GmailUnreadTool,
        )
        server._tool_registry.register(GmailUnreadTool())
        server._tool_registry.register(GmailSearchTool())
        server._tool_registry.register(GmailReadTool())
        server._tool_registry.register(GmailSendTool())
        server._tool_registry.register(GmailArchiveTool())
        logger.info("Gmail tools registered (5)")
    except Exception as e:
        logger.warning("Gmail tools not available: %s", e)
