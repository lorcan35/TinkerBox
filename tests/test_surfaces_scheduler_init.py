"""Tests for ``dragon_voice.lifecycle.surfaces_scheduler_init``.

Pin every layer of the failure-isolation matrix + the
SqliteNotificationStore-default contract (#131 ε2 closure).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dragon_voice.lifecycle.surfaces_scheduler_init import (
    init_surfaces_and_scheduler,
)


def _make_server(*, with_registry: bool = True) -> SimpleNamespace:
    server = SimpleNamespace(
        _db=MagicMock(),
        _session_mgr=MagicMock(),
        _surface_mgr=None,
        _scheduler_mgr=None,
        _scheduler_store=None,
        _tool_registry=MagicMock() if with_registry else None,
    )
    if with_registry:
        server._tool_registry.register = MagicMock()
    return server


# ─── SurfaceManager init (always succeeds) ───────────────────


class TestSurfaceManagerInit:
    @pytest.mark.asyncio
    async def test_surface_mgr_always_initialized(self):
        """SurfaceManager has no required deps — always
        constructs.  Pin so a future refactor that adds deps
        can't silently break the always-init invariant."""
        # Patch scheduler bits so the test doesn't need a live
        # SQLite-vec install; SurfaceManager construction is the
        # thing under test.
        server = _make_server()

        with patch(
            "dragon_voice.scheduler.SqliteNotificationStore"
        ), patch(
            "dragon_voice.scheduler.SchedulerManager"
        ) as SchedMgr:
            sched = MagicMock()
            sched.start = AsyncMock()
            SchedMgr.return_value = sched

            await init_surfaces_and_scheduler(server)

        assert server._surface_mgr is not None
        # Class check via the same import path the production
        # code uses (avoids identity drift from sys.modules
        # patching tricks).
        assert type(server._surface_mgr).__name__ == "SurfaceManager"


# ─── SchedulerManager + store fallback ───────────────────────


class TestSchedulerInit:
    @pytest.mark.asyncio
    async def test_sqlite_store_default_when_init_succeeds(self):
        """ε2 / #131 closure: SqliteNotificationStore is the default
        store so notifications survive Dragon restart."""
        server = _make_server()

        with patch(
            "dragon_voice.scheduler.SqliteNotificationStore"
        ) as Sqlite, patch(
            "dragon_voice.scheduler.SchedulerManager"
        ) as SchedMgr:
            sqlite_inst = MagicMock(name="sqlite_store")
            Sqlite.return_value = sqlite_inst
            sched_inst = MagicMock(name="sched")
            sched_inst.start = AsyncMock()
            SchedMgr.return_value = sched_inst

            await init_surfaces_and_scheduler(server)

        assert server._scheduler_store is sqlite_inst
        assert server._scheduler_mgr is sched_inst
        sched_inst.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sqlite_init_failure_falls_back_to_in_memory(self):
        """ε2 closure: SqliteNotificationStore failure → fall
        back to in-memory store rather than disabling scheduler
        entirely.  Notifications won't survive restart this run
        but new ones still fire."""
        server = _make_server()

        with patch(
            "dragon_voice.scheduler.SqliteNotificationStore"
        ) as Sqlite, patch(
            "dragon_voice.scheduler.InMemoryNotificationStore"
        ) as InMem, patch(
            "dragon_voice.scheduler.SchedulerManager"
        ) as SchedMgr:
            Sqlite.side_effect = RuntimeError("sqlite-vec missing")
            inmem_inst = MagicMock(name="inmem")
            InMem.return_value = inmem_inst
            sched_inst = MagicMock()
            sched_inst.start = AsyncMock()
            SchedMgr.return_value = sched_inst

            await init_surfaces_and_scheduler(server)

        assert server._scheduler_store is inmem_inst
        # Scheduler still wired with the fallback store
        assert server._scheduler_mgr is sched_inst

    @pytest.mark.asyncio
    async def test_scheduler_module_import_failure_disables_scheduler(self):
        """When the whole scheduler subsystem is unavailable
        (import error), `_scheduler_mgr` stays None so downstream
        skill registration silently skips."""
        server = _make_server()

        # Force the scheduler import to fail by patching the module
        # to raise on any attribute access.
        with patch.dict(
            "sys.modules", {"dragon_voice.scheduler": None},
        ):
            await init_surfaces_and_scheduler(server)

        assert server._scheduler_mgr is None


# ─── Skill registration (TimesenseTool, QuickPollTool, ScheduleReminderTool) ──


class TestSkillRegistration:
    @pytest.mark.asyncio
    async def test_widget_tools_registered_when_registry_present(self):
        server = _make_server(with_registry=True)

        with patch(
            "dragon_voice.tools.timesense_tool.TimesenseTool"
        ) as Timesense, patch(
            "dragon_voice.tools.quick_poll_tool.QuickPollTool"
        ) as QuickPoll, patch(
            "dragon_voice.tools.schedule_reminder_tool.ScheduleReminderTool"
        ) as Sched, patch(
            "dragon_voice.scheduler.SqliteNotificationStore"
        ), patch(
            "dragon_voice.scheduler.SchedulerManager"
        ) as SchedMgr:
            ts_inst = MagicMock(name="ts")
            qp_inst = MagicMock(name="qp")
            sr_inst = MagicMock(name="sr")
            Timesense.return_value = ts_inst
            QuickPoll.return_value = qp_inst
            Sched.return_value = sr_inst
            sched_mgr = MagicMock()
            sched_mgr.start = AsyncMock()
            SchedMgr.return_value = sched_mgr

            await init_surfaces_and_scheduler(server)

        # All three tools registered
        registered = [
            c.args[0] for c in server._tool_registry.register.call_args_list
        ]
        assert ts_inst in registered
        assert qp_inst in registered
        assert sr_inst in registered

    @pytest.mark.asyncio
    async def test_no_registry_skips_skill_registration(self):
        """When agentic init failed, _tool_registry is None — skip
        the skill registration entirely.  Scheduler still init'd
        for the REST API surface."""
        server = _make_server(with_registry=False)

        with patch(
            "dragon_voice.scheduler.SqliteNotificationStore"
        ), patch(
            "dragon_voice.scheduler.SchedulerManager"
        ) as SchedMgr:
            sched = MagicMock()
            sched.start = AsyncMock()
            SchedMgr.return_value = sched

            # Must NOT raise (no registry to register against).
            await init_surfaces_and_scheduler(server)

        # SurfaceManager + SchedulerManager still init'd
        assert server._surface_mgr is not None
        assert server._scheduler_mgr is sched

    @pytest.mark.asyncio
    async def test_scheduler_tool_skipped_when_scheduler_init_failed(self):
        """If scheduler init failed (_scheduler_mgr is None),
        ScheduleReminderTool MUST NOT register — the tool
        constructor expects a live scheduler manager."""
        server = _make_server(with_registry=True)

        # Force scheduler init failure
        with patch.dict("sys.modules", {"dragon_voice.scheduler": None}):
            await init_surfaces_and_scheduler(server)

        # ScheduleReminderTool was NOT registered
        for c in server._tool_registry.register.call_args_list:
            assert "ScheduleReminderTool" not in type(c.args[0]).__name__

    @pytest.mark.asyncio
    async def test_timesense_failure_does_not_block_scheduler_tool(self):
        """Layered try/except: TimesenseTool registration failing
        MUST NOT prevent ScheduleReminderTool from registering
        (separate try blocks)."""
        server = _make_server(with_registry=True)

        with patch(
            "dragon_voice.tools.timesense_tool.TimesenseTool"
        ) as Timesense, patch(
            "dragon_voice.tools.schedule_reminder_tool.ScheduleReminderTool"
        ) as Sched, patch(
            "dragon_voice.scheduler.SqliteNotificationStore"
        ), patch(
            "dragon_voice.scheduler.SchedulerManager"
        ) as SchedMgr:
            Timesense.side_effect = ImportError("timesense missing")
            sr_inst = MagicMock(name="sr")
            Sched.return_value = sr_inst
            sched_mgr = MagicMock()
            sched_mgr.start = AsyncMock()
            SchedMgr.return_value = sched_mgr

            await init_surfaces_and_scheduler(server)

        # ScheduleReminderTool still registered despite Timesense
        # failure — separate try block invariant.
        registered = [
            c.args[0] for c in server._tool_registry.register.call_args_list
        ]
        assert sr_inst in registered

    @pytest.mark.asyncio
    async def test_scheduler_tool_failure_does_not_propagate(self):
        """ScheduleReminderTool registration failure logs at
        WARNING but doesn't take down boot."""
        server = _make_server(with_registry=True)

        with patch(
            "dragon_voice.tools.timesense_tool.TimesenseTool"
        ), patch(
            "dragon_voice.tools.quick_poll_tool.QuickPollTool"
        ), patch(
            "dragon_voice.tools.schedule_reminder_tool.ScheduleReminderTool"
        ) as Sched, patch(
            "dragon_voice.scheduler.SqliteNotificationStore"
        ), patch(
            "dragon_voice.scheduler.SchedulerManager"
        ) as SchedMgr:
            Sched.side_effect = ImportError("scheduler tool missing")
            sched_mgr = MagicMock()
            sched_mgr.start = AsyncMock()
            SchedMgr.return_value = sched_mgr

            # Must NOT raise.
            await init_surfaces_and_scheduler(server)
