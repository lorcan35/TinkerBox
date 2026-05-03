"""Tests for ``dragon_voice.disconnect_handler``.

Pin every step of the cleanup chain so a future refactor can't
accidentally drop one of:
  * bg_task cancel (W14-C06 — Piper subproc would leak)
  * handler_task cancel (Phase 1 / #91 — slow turn would keep
    streaming to dead session's DB)
  * surface unregister (Phase 4g)
  * session pause (NOT end — must be resumable)
  * multi-tab safety on device.set_device_online(False)
  * pipeline shutdown
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.disconnect_handler import handle_disconnect


def _make_session_mgr() -> MagicMock:
    mgr = MagicMock()
    mgr.pause_session = AsyncMock()
    return mgr


def _make_db() -> MagicMock:
    db = MagicMock()
    db.set_device_online = AsyncMock()
    db.add_event = AsyncMock()
    return db


def _make_surface_mgr() -> MagicMock:
    mgr = MagicMock()
    mgr.unregister_session = AsyncMock()
    return mgr


def _make_pipeline() -> MagicMock:
    p = MagicMock()
    p.shutdown = AsyncMock()
    return p


def _make_inflight_task() -> asyncio.Task:
    async def _hang():
        await asyncio.sleep(60)
    return asyncio.create_task(_hang())


# ─── bg_tasks ────────────────────────────────────────────────


class TestBgTasksCancel:
    @pytest.mark.asyncio
    async def test_bg_tasks_cancelled_and_awaited(self):
        t1 = _make_inflight_task()
        t2 = _make_inflight_task()
        bg_tasks = {t1, t2}
        await handle_disconnect(
            {"bg_tasks": bg_tasks, "session_id": "s"},
            active_connections={},
            session_mgr=_make_session_mgr(),
            db=_make_db(),
            surface_mgr=None,
        )
        assert t1.cancelled()
        assert t2.cancelled()

    @pytest.mark.asyncio
    async def test_no_bg_tasks_is_silent_noop(self):
        await handle_disconnect(
            {"session_id": "s"},
            active_connections={},
            session_mgr=_make_session_mgr(),
            db=_make_db(),
            surface_mgr=None,
        )

    @pytest.mark.asyncio
    async def test_done_bg_tasks_handled_gracefully(self):
        async def _quick():
            return "done"
        finished = asyncio.create_task(_quick())
        await asyncio.sleep(0)
        assert finished.done()

        # Must not raise — gather with return_exceptions handles it.
        await handle_disconnect(
            {"bg_tasks": {finished}, "session_id": "s"},
            active_connections={},
            session_mgr=_make_session_mgr(),
            db=_make_db(),
            surface_mgr=None,
        )


# ─── handler_tasks (Phase 1 / #91) ───────────────────────────


class TestHandlerTasksCancel:
    @pytest.mark.asyncio
    async def test_inflight_handler_tasks_cancelled(self):
        text_t = _make_inflight_task()
        media_t = _make_inflight_task()
        await handle_disconnect(
            {
                "handler_tasks": {"text": text_t, "media": media_t},
                "session_id": "s",
            },
            active_connections={},
            session_mgr=_make_session_mgr(),
            db=_make_db(),
            surface_mgr=None,
        )
        assert text_t.cancelled()
        assert media_t.cancelled()

    @pytest.mark.asyncio
    async def test_done_handler_tasks_skipped(self):
        async def _quick():
            return None
        finished = asyncio.create_task(_quick())
        await asyncio.sleep(0)
        await handle_disconnect(
            {
                "handler_tasks": {"text": finished, "media": None},
                "session_id": "s",
            },
            active_connections={},
            session_mgr=_make_session_mgr(),
            db=_make_db(),
            surface_mgr=None,
        )
        # No assertion — just must not crash on done/None tasks


# ─── Surface unregister ──────────────────────────────────────


class TestSurfaceUnregister:
    @pytest.mark.asyncio
    async def test_surface_unregister_invoked_with_session_id(self):
        surface = _make_surface_mgr()
        await handle_disconnect(
            {"session_id": "sess-A"},
            active_connections={},
            session_mgr=_make_session_mgr(),
            db=_make_db(),
            surface_mgr=surface,
        )
        surface.unregister_session.assert_awaited_once_with("sess-A")

    @pytest.mark.asyncio
    async def test_no_surface_mgr_skips_unregister(self):
        # Must not raise.
        await handle_disconnect(
            {"session_id": "s"},
            active_connections={},
            session_mgr=_make_session_mgr(),
            db=_make_db(),
            surface_mgr=None,
        )

    @pytest.mark.asyncio
    async def test_no_session_id_skips_unregister(self):
        surface = _make_surface_mgr()
        await handle_disconnect(
            {},  # no session_id
            active_connections={},
            session_mgr=_make_session_mgr(),
            db=_make_db(),
            surface_mgr=surface,
        )
        surface.unregister_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unregister_failure_does_not_propagate(self):
        surface = MagicMock()
        surface.unregister_session = AsyncMock(
            side_effect=RuntimeError("surface dead"),
        )
        # Must not raise.
        await handle_disconnect(
            {"session_id": "s"},
            active_connections={},
            session_mgr=_make_session_mgr(),
            db=_make_db(),
            surface_mgr=surface,
        )


# ─── Session pause ───────────────────────────────────────────


class TestSessionPause:
    @pytest.mark.asyncio
    async def test_session_paused_not_ended(self):
        """Pin: pause_session is called, NOT end_session.  Tab5
        must be able to reconnect with the same session_id and
        resume."""
        mgr = _make_session_mgr()
        mgr.end_session = AsyncMock()  # pin: NOT called

        await handle_disconnect(
            {"session_id": "sess-1", "device_id": "d"},
            active_connections={},
            session_mgr=mgr,
            db=_make_db(),
            surface_mgr=None,
        )
        mgr.pause_session.assert_awaited_once_with("sess-1")
        mgr.end_session.assert_not_awaited()


# ─── Device offline (multi-tab safety) ───────────────────────


class TestDeviceOffline:
    @pytest.mark.asyncio
    async def test_device_marked_offline_when_no_other_active_conn(self):
        db = _make_db()
        await handle_disconnect(
            {"session_id": "s", "device_id": "tab5-A", "ws_id": "ws-1"},
            active_connections={},  # no other conns
            session_mgr=_make_session_mgr(),
            db=db,
            surface_mgr=None,
        )
        db.set_device_online.assert_awaited_once_with("tab5-A", False)
        db.add_event.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_device_stays_online_when_another_active_conn_exists(self):
        """Multi-tab safety: a second WS for the same device must
        keep the device marked online when one disconnects."""
        db = _make_db()
        other_conn = {
            "device_id": "tab5-A",
            "registered": True,
        }
        await handle_disconnect(
            {"session_id": "s", "device_id": "tab5-A", "ws_id": "ws-1"},
            active_connections={"ws-2": other_conn},
            session_mgr=_make_session_mgr(),
            db=db,
            surface_mgr=None,
        )
        db.set_device_online.assert_not_awaited()
        db.add_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unregistered_other_conn_does_not_keep_online(self):
        """Same device_id but unregistered (e.g. mid-handshake) —
        does NOT count as an active connection for offline-gate
        purposes."""
        db = _make_db()
        unreg_conn = {"device_id": "tab5-A", "registered": False}
        await handle_disconnect(
            {"session_id": "s", "device_id": "tab5-A", "ws_id": "ws-1"},
            active_connections={"ws-2": unreg_conn},
            session_mgr=_make_session_mgr(),
            db=db,
            surface_mgr=None,
        )
        db.set_device_online.assert_awaited_once_with("tab5-A", False)

    @pytest.mark.asyncio
    async def test_different_device_does_not_keep_online(self):
        """Another device_id active → first device still goes offline."""
        db = _make_db()
        other_conn = {"device_id": "tab5-B", "registered": True}
        await handle_disconnect(
            {"session_id": "s", "device_id": "tab5-A", "ws_id": "ws-1"},
            active_connections={"ws-2": other_conn},
            session_mgr=_make_session_mgr(),
            db=db,
            surface_mgr=None,
        )
        db.set_device_online.assert_awaited_once_with("tab5-A", False)

    @pytest.mark.asyncio
    async def test_no_db_skips_offline(self):
        # Must not raise.
        await handle_disconnect(
            {"session_id": "s", "device_id": "d"},
            active_connections={},
            session_mgr=_make_session_mgr(),
            db=None,
            surface_mgr=None,
        )

    @pytest.mark.asyncio
    async def test_db_failure_does_not_propagate(self):
        """DB may be closed during server shutdown — failures
        must not propagate (otherwise the disconnect chain
        breaks mid-walk and pipeline doesn't shut down)."""
        db = MagicMock()
        db.set_device_online = AsyncMock(side_effect=RuntimeError("db closed"))
        db.add_event = AsyncMock()
        pipeline = _make_pipeline()

        await handle_disconnect(
            {
                "session_id": "s", "device_id": "d", "ws_id": "ws",
                "pipeline": pipeline,
            },
            active_connections={},
            session_mgr=_make_session_mgr(),
            db=db,
            surface_mgr=None,
        )
        # Pipeline shutdown still ran despite DB failure.
        pipeline.shutdown.assert_awaited_once()


# ─── Pipeline shutdown ───────────────────────────────────────


class TestPipelineShutdown:
    @pytest.mark.asyncio
    async def test_pipeline_shutdown_invoked(self):
        pipeline = _make_pipeline()
        await handle_disconnect(
            {"session_id": "s", "pipeline": pipeline},
            active_connections={},
            session_mgr=_make_session_mgr(),
            db=_make_db(),
            surface_mgr=None,
        )
        pipeline.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_pipeline_silent_skip(self):
        """Boot race: disconnect arrives before register attached
        the pipeline.  Must not crash."""
        await handle_disconnect(
            {"session_id": "s"},
            active_connections={},
            session_mgr=_make_session_mgr(),
            db=_make_db(),
            surface_mgr=None,
        )


# ─── End-to-end ──────────────────────────────────────────────


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_full_chain_runs_in_order(self):
        """Pin: every step runs even when no failures along the
        way — bg cancel → handler cancel → surface unreg →
        session pause → device offline → pipeline shutdown."""
        text_t = _make_inflight_task()
        bg_t = _make_inflight_task()
        mgr = _make_session_mgr()
        db = _make_db()
        surface = _make_surface_mgr()
        pipeline = _make_pipeline()

        await handle_disconnect(
            {
                "session_id": "sess",
                "device_id": "dev",
                "ws_id": "ws",
                "pipeline": pipeline,
                "bg_tasks": {bg_t},
                "handler_tasks": {"text": text_t},
            },
            active_connections={},
            session_mgr=mgr,
            db=db,
            surface_mgr=surface,
        )

        assert bg_t.cancelled()
        assert text_t.cancelled()
        surface.unregister_session.assert_awaited_once_with("sess")
        mgr.pause_session.assert_awaited_once_with("sess")
        db.set_device_online.assert_awaited_once_with("dev", False)
        pipeline.shutdown.assert_awaited_once()
