"""Surfaces + Scheduler init step for `run_startup`.

Wave 23 SOLID-audit follow-up — twenty-seventh sub-extract.
Second slice from `dragon_voice/lifecycle/startup.py` (audit
SRP-6: `run_startup` decomposition; PR #252 extracted the
agentic init).

Owns the boot wiring for:
  * `SurfaceManager` (Tab5 widget surface registry — where
    widget_action events from `widget_action_handler` land).
  * `SchedulerManager` (Phase 5 in-process notification
    scheduler) + its persistence store (SqliteNotificationStore
    by default, InMemoryNotificationStore fallback).
  * The widget-emitting tools that depend on SurfaceManager
    (TimesenseTool — pomodoro/timer with widget_live progress;
    QuickPollTool — Wave 12 declarative-widget skill SDK
    reference).
  * The scheduler-emitting tool (ScheduleReminderTool — issue
    #134 closure for "set me a reminder" intents).

Pre-extract this 70-LOC chunk lived inline in `run_startup`
between the agentic init and the conversation engine.  Now
lives in its own dedicated module with its own tests.

## API

```python
await init_surfaces_and_scheduler(server)
```

Mutates ``server._surface_mgr``, ``server._scheduler_mgr``,
``server._scheduler_store``, and registers TimesenseTool +
QuickPollTool + ScheduleReminderTool on
``server._tool_registry`` (when the registry exists — the
agentic init may have failed; we silently skip the widget tools
in that case).

## Failure-isolation matrix

The audit-D8 invariant is layered try/except — a failure in
one sub-step doesn't take down the others:

  * Scheduler init failure → `_scheduler_mgr` left as None;
    skill registration silently skips.
  * SqliteNotificationStore init failure → falls back to
    InMemoryNotificationStore (notifications won't survive
    restart this run, but new notifications still fire).
  * TimesenseTool / QuickPollTool registration failure → both
    skipped, scheduler tool registration still attempts.
  * ScheduleReminderTool registration failure → just that tool
    skipped; rest of the boot chain continues.

## ε2 SqliteNotificationStore default (#131 closure)

Pre-ε2 the default store was InMemoryNotificationStore, which
meant a Dragon restart silently lost all pending reminders
(user set "remind me at 6am tomorrow", Dragon restarted at 4am,
6am came and went with no notification).  ε2 made the SQLite
store the default; boot replay (in `manager.start`) reads
`list_due(now)` so any due-but-unfired notifications from the
prior process get rescheduled within the 15-minute
REPLAY_WINDOW_SECONDS cap (RFC R8).
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def init_surfaces_and_scheduler(server: Any) -> None:
    """Initialize SurfaceManager + SchedulerManager + the
    widget/scheduler-emitting tools.

    Run AFTER `init_agentic_modules` (depends on
    `_tool_registry` for the skill registration step) but
    BEFORE `init_conversation_engine` (which doesn't depend on
    these but expects the registry to be fully populated for
    the system-prompt generation).

    Side effects on ``server``:
      * `_surface_mgr` — new SurfaceManager instance
      * `_scheduler_mgr` — SchedulerManager (or None on init failure)
      * `_scheduler_store` — Sqlite or in-memory notification store
      * `_tool_registry` — TimesenseTool, QuickPollTool, and
        ScheduleReminderTool registered (when applicable)

    Failure isolation: see module docstring for the layered
    try/except invariant.  No failure here blocks boot.
    """
    # v4·D Phase 4g stability fix (audit P0 #1): SurfaceManager so
    # Tab5 widget_action events have somewhere to land.
    from dragon_voice.surfaces import SurfaceManager
    server._surface_mgr = SurfaceManager()
    logger.info("SurfaceManager initialized")

    # Phase 5 ε1a/ε2 (issues #128, #131): in-process scheduler.
    # ε2: SqliteNotificationStore default; in-memory fallback if
    # SQLite store init fails.
    server._scheduler_mgr = None
    try:
        from dragon_voice.scheduler import (
            InMemoryNotificationStore,
            SchedulerManager,
            SqliteNotificationStore,
        )
        try:
            server._scheduler_store = SqliteNotificationStore(server._db)
        except Exception as e:
            logger.warning(
                "SqliteNotificationStore init failed: %s — "
                "falling back to InMemoryNotificationStore "
                "(notifications won't survive restart this run)", e,
            )
            server._scheduler_store = InMemoryNotificationStore()
        server._scheduler_mgr = SchedulerManager(
            store=server._scheduler_store,
            surface_mgr=server._surface_mgr,
            session_mgr=server._session_mgr,
        )
        await server._scheduler_mgr.start()
        logger.info(
            "SchedulerManager initialized (store=%s)",
            type(server._scheduler_store).__name__,
        )
    except Exception as e:
        logger.warning("SchedulerManager init failed: %s", e)

    # Register widget-emitting tools AFTER surface_mgr exists.
    # When the agentic init failed, _tool_registry is None — skip
    # the skill registration entirely.
    if server._tool_registry is not None:
        _register_surface_tools(server)
        # Scheduler tool needs both _tool_registry AND _scheduler_mgr.
        if server._scheduler_mgr is not None:
            _register_scheduler_tool(server)


def _register_surface_tools(server: Any) -> None:
    """Register TimesenseTool + QuickPollTool on the registry.

    Layered try/except: failure of either tool import / register
    logs at WARNING but doesn't block the rest of the boot chain.
    """
    try:
        from dragon_voice.tools.timesense_tool import TimesenseTool
        server._tool_registry.register(TimesenseTool(server._surface_mgr))
        logger.info("TimesenseTool registered (widget emitter)")
        # Wave 12 skill SDK reference — declarative
        # `surface.prompt(on_action=handler)` style.
        from dragon_voice.tools.quick_poll_tool import QuickPollTool
        server._tool_registry.register(QuickPollTool(server._surface_mgr))
        logger.info("QuickPollTool registered (declarative widget skill)")
    except Exception as e:
        logger.warning("TimesenseTool registration failed: %s", e)


def _register_scheduler_tool(server: Any) -> None:
    """Register ScheduleReminderTool on the registry.

    Separate try block from `_register_surface_tools` so a tool-
    init error doesn't take down the rest of the registry.
    Issue #134 closure: this tool is what makes "set me a
    reminder" intents work.
    """
    try:
        from dragon_voice.tools.schedule_reminder_tool import (
            ScheduleReminderTool,
        )
        server._tool_registry.register(
            ScheduleReminderTool(server._scheduler_mgr, server._db)
        )
        logger.info("ScheduleReminderTool registered (scheduler skill)")
    except Exception as e:
        logger.warning("ScheduleReminderTool registration failed: %s", e)
