"""Tests for the three SRP-6 finalisation modules:
``dragon_voice.lifecycle.notes_init``,
``dragon_voice.lifecycle.mcp_init``,
``dragon_voice.lifecycle.background_tasks_init``.

Pin the failure-isolation invariant for each (Notes/MCP/purge
failure must NOT block boot) + the task-handle contract (the
three task fields must all be populated for `run_shutdown` to
cancel them).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_server(*, retention_days: int = 30) -> SimpleNamespace:
    server = SimpleNamespace(
        _db=MagicMock(),
        _config=SimpleNamespace(
            database=SimpleNamespace(
                message_retention_days=retention_days,
            ),
            mcp_servers=[],
        ),
        _tool_registry=MagicMock(),
        _notes_svc=None,
        _mem_warn_mb=400,
        _mem_crit_mb=800,
        _media_store=MagicMock(),
        _purge_task=None,
        _media_cleanup_task=None,
        _memory_monitor_task=None,
    )
    server._tool_registry.register = MagicMock()
    server._db.purge_old_messages = AsyncMock(
        return_value={"messages": 0, "events": 0},
    )
    return server


# ─── notes_init ──────────────────────────────────────────────


class TestNotesInit:
    @pytest.mark.asyncio
    async def test_notes_svc_set_and_routes_registered(self):
        from dragon_voice.lifecycle.notes_init import init_notes_module

        server = _make_server()
        app = MagicMock()

        with patch(
            "dragon_voice.notes.api.setup_routes"
        ) as setup_routes, patch(
            "dragon_voice.notes.db.NotesDB"
        ), patch(
            "dragon_voice.notes.service.NotesService"
        ) as NotesSvc:
            svc_inst = MagicMock()
            svc_inst.initialize = AsyncMock()
            NotesSvc.return_value = svc_inst

            await init_notes_module(server, app)

        assert server._notes_svc is svc_inst
        setup_routes.assert_called_once_with(app, svc_inst)

    @pytest.mark.asyncio
    async def test_note_tool_registered_when_registry_and_svc_present(self):
        from dragon_voice.lifecycle.notes_init import init_notes_module

        server = _make_server()
        app = MagicMock()

        with patch(
            "dragon_voice.notes.api.setup_routes"
        ), patch(
            "dragon_voice.notes.db.NotesDB"
        ), patch(
            "dragon_voice.notes.service.NotesService"
        ) as NotesSvc, patch(
            "dragon_voice.tools.note_tool.NoteTool"
        ) as NoteTool:
            svc = MagicMock()
            svc.initialize = AsyncMock()
            NotesSvc.return_value = svc
            note_tool = MagicMock()
            NoteTool.return_value = note_tool

            await init_notes_module(server, app)

        # NoteTool was constructed with the notes service AND
        # registered on the registry.
        NoteTool.assert_called_once_with(svc)
        server._tool_registry.register.assert_called_with(note_tool)

    @pytest.mark.asyncio
    async def test_no_registry_skips_note_tool_but_still_registers_routes(self):
        from dragon_voice.lifecycle.notes_init import init_notes_module

        server = _make_server()
        server._tool_registry = None  # agentic init failed
        app = MagicMock()

        with patch(
            "dragon_voice.notes.api.setup_routes"
        ) as setup_routes, patch(
            "dragon_voice.notes.db.NotesDB"
        ), patch(
            "dragon_voice.notes.service.NotesService"
        ) as NotesSvc:
            svc = MagicMock()
            svc.initialize = AsyncMock()
            NotesSvc.return_value = svc

            await init_notes_module(server, app)

        # Routes still registered (REST API matters even
        # without LLM tool integration)
        setup_routes.assert_called_once()

    @pytest.mark.asyncio
    async def test_init_failure_does_not_block_boot(self):
        """Pin: a missing optional dep (no notes module
        installed) logs at WARNING and returns silently."""
        from dragon_voice.lifecycle.notes_init import init_notes_module

        server = _make_server()
        app = MagicMock()

        with patch(
            "dragon_voice.notes.service.NotesService"
        ) as NotesSvc:
            NotesSvc.side_effect = ImportError("notes module missing")
            # Must NOT raise.
            await init_notes_module(server, app)

        # _notes_svc left as initialized state (None)
        assert server._notes_svc is None


# ─── mcp_init ────────────────────────────────────────────────


class TestMcpInit:
    @pytest.mark.asyncio
    async def test_no_mcp_servers_is_silent_noop(self):
        from dragon_voice.lifecycle.mcp_init import init_mcp_bridges

        server = _make_server()
        # mcp_servers = [] (default in _make_server)

        with patch(
            "dragon_voice.mcp.bridge.bridge_mcp_server"
        ) as bridge:
            await init_mcp_bridges(server)

        bridge.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_each_mcp_server_bridged(self):
        from dragon_voice.lifecycle.mcp_init import init_mcp_bridges

        server = _make_server()
        server._config.mcp_servers = [
            {"name": "tool_box", "url": "http://x", "token": "t1"},
            {"name": "skill_lib", "url": "http://y"},
        ]

        with patch(
            "dragon_voice.mcp.bridge.bridge_mcp_server"
        ) as bridge:
            bridge.return_value = 5  # tools per server

            await init_mcp_bridges(server)

        assert bridge.await_count == 2
        # First server has token; second doesn't (None)
        first_kwargs = bridge.await_args_list[0].kwargs
        assert first_kwargs["name"] == "tool_box"
        assert first_kwargs["url"] == "http://x"
        assert first_kwargs["token"] == "t1"
        second_kwargs = bridge.await_args_list[1].kwargs
        assert second_kwargs["name"] == "skill_lib"
        assert second_kwargs["token"] is None

    @pytest.mark.asyncio
    async def test_missing_mcp_servers_attr_uses_empty_list(self):
        """getattr fallback pin — older configs without the attr
        must not crash."""
        from dragon_voice.lifecycle.mcp_init import init_mcp_bridges

        server = _make_server()
        # Strip the attr entirely
        del server._config.mcp_servers

        with patch(
            "dragon_voice.mcp.bridge.bridge_mcp_server"
        ) as bridge:
            await init_mcp_bridges(server)

        bridge.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bridge_failure_does_not_propagate(self):
        from dragon_voice.lifecycle.mcp_init import init_mcp_bridges

        server = _make_server()
        server._config.mcp_servers = [{"name": "broken", "url": "http://x"}]

        with patch(
            "dragon_voice.mcp.bridge.bridge_mcp_server"
        ) as bridge:
            bridge.side_effect = ConnectionRefusedError("MCP down")
            # Must NOT raise.
            await init_mcp_bridges(server)


# ─── background_tasks_init ───────────────────────────────────


class TestBackgroundTasksInit:
    @pytest.mark.asyncio
    async def test_three_task_handles_assigned(self):
        from dragon_voice.lifecycle.background_tasks_init import (
            init_background_tasks,
        )

        server = _make_server(retention_days=30)

        with patch(
            "dragon_voice.lifecycle.purge.media_cleanup_loop",
            new=AsyncMock(),
        ), patch(
            "dragon_voice.lifecycle.monitors.memory_monitor_loop",
            new=AsyncMock(),
        ), patch(
            "dragon_voice.lifecycle.monitors.get_rss_mb",
            return_value=512.0,
        ):
            init_background_tasks(server)
            # Wait one tick to let create_task schedule
            await asyncio.sleep(0)

        assert server._purge_task is not None
        assert server._media_cleanup_task is not None
        assert server._memory_monitor_task is not None

        # Cleanup so tests don't leak tasks
        for t in (
            server._purge_task,
            server._media_cleanup_task,
            server._memory_monitor_task,
        ):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    @pytest.mark.asyncio
    async def test_retention_zero_skips_purge_task(self):
        """Pin: retention_days=0 → no purge task spawned (admin
        opted out of message retention)."""
        from dragon_voice.lifecycle.background_tasks_init import (
            init_background_tasks,
        )

        server = _make_server(retention_days=0)

        with patch(
            "dragon_voice.lifecycle.purge.media_cleanup_loop",
            new=AsyncMock(),
        ), patch(
            "dragon_voice.lifecycle.monitors.memory_monitor_loop",
            new=AsyncMock(),
        ), patch(
            "dragon_voice.lifecycle.monitors.get_rss_mb",
            return_value=512.0,
        ):
            init_background_tasks(server)
            await asyncio.sleep(0)

        # _purge_task NOT assigned (still None)
        assert server._purge_task is None
        # Other two still assigned
        assert server._media_cleanup_task is not None
        assert server._memory_monitor_task is not None

        for t in (
            server._media_cleanup_task,
            server._memory_monitor_task,
        ):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    @pytest.mark.asyncio
    async def test_startup_purge_failure_does_not_block_periodic_loop(self):
        """Pin invariant: a transient DB issue at startup-purge
        time MUST NOT prevent the periodic loop from running.
        The wrapped task swallows + falls through."""
        from dragon_voice.lifecycle.background_tasks_init import (
            _run_startup_purge_then_loop,
        )

        server = _make_server()
        server._db.purge_old_messages = AsyncMock(
            side_effect=RuntimeError("DB locked"),
        )

        loop_invoked = asyncio.Event()

        async def _fake_loop(*args, **kwargs):
            loop_invoked.set()

        with patch(
            "dragon_voice.lifecycle.purge.periodic_purge_loop",
            new=_fake_loop,
        ):
            await _run_startup_purge_then_loop(server, 30)

        assert loop_invoked.is_set()
