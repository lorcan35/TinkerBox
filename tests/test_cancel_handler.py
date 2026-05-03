"""Tests for ``dragon_voice.cancel_handler``.

Pin every step of the cancel chain so a future refactor can't
accidentally drop the in-flight task cancel, the pipeline kill,
the deferred-widget discard, or the cancel_ack.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.cancel_handler import handle_cancel_command


def _make_ws() -> MagicMock:
    ws = MagicMock()
    ws.closed = False
    ws.send_json = AsyncMock()
    return ws


def _make_safe_send_json() -> AsyncMock:
    return AsyncMock(return_value=True)


def _make_pipeline() -> MagicMock:
    p = MagicMock()
    p.cancel = AsyncMock()
    return p


def _make_inflight_task() -> asyncio.Task:
    """A real asyncio.Task that hangs forever — we'll cancel it."""
    async def _hang():
        await asyncio.sleep(60)
    return asyncio.create_task(_hang())


# ─── No-op (nothing to cancel) ────────────────────────────────


class TestNoOp:
    @pytest.mark.asyncio
    async def test_no_tasks_no_pipeline_no_widgets_still_acks(self):
        """User hit STOP after the turn already finished — nothing
        in flight, but we still send `cancel_ack` with an empty
        list so Tab5 has a positive signal."""
        ws = _make_ws()
        send = _make_safe_send_json()

        await handle_cancel_command(
            ws,
            ws_id="ws1",
            conn_state={"session_id": "s1"},
            surface_mgr=None,
            safe_send_json=send,
        )

        send.assert_awaited_once()
        ack = send.await_args.args[1]
        assert ack["type"] == "cancel_ack"
        assert ack["cancelled"] == []


# ─── Handler-task cancellation ────────────────────────────────


class TestHandlerTaskCancel:
    @pytest.mark.asyncio
    async def test_cancels_inflight_text_task_and_clears_slot(self):
        ws = _make_ws()
        send = _make_safe_send_json()
        text_task = _make_inflight_task()
        handler_tasks = {"text": text_task, "media": None, "config": None}

        await handle_cancel_command(
            ws,
            ws_id="ws2",
            conn_state={
                "session_id": "s",
                "handler_tasks": handler_tasks,
            },
            surface_mgr=None,
            safe_send_json=send,
        )

        # Task was cancelled
        assert text_task.cancelled()
        # Slot reset to None (so dispatcher knows it's free)
        assert handler_tasks["text"] is None
        # cancel_ack lists the slot
        ack = send.await_args.args[1]
        assert "text" in ack["cancelled"]

    @pytest.mark.asyncio
    async def test_cancels_all_three_slots_in_order(self):
        ws = _make_ws()
        send = _make_safe_send_json()
        text_t = _make_inflight_task()
        media_t = _make_inflight_task()
        config_t = _make_inflight_task()
        handler_tasks = {"text": text_t, "media": media_t, "config": config_t}

        await handle_cancel_command(
            ws,
            ws_id="ws3",
            conn_state={
                "session_id": "s",
                "handler_tasks": handler_tasks,
            },
            surface_mgr=None,
            safe_send_json=send,
        )

        for t in (text_t, media_t, config_t):
            assert t.cancelled()

        # Order pinned: text, media, config (legacy walk order
        # — preserved for log-line stability).
        ack = send.await_args.args[1]
        assert ack["cancelled"][:3] == ["text", "media", "config"]

    @pytest.mark.asyncio
    async def test_done_task_is_skipped(self):
        """A task that already finished must NOT get cancelled
        (idempotent on completed tasks)."""
        ws = _make_ws()
        send = _make_safe_send_json()

        async def _quick():
            return "done"

        finished = asyncio.create_task(_quick())
        await asyncio.sleep(0)  # let it complete
        assert finished.done()

        handler_tasks = {"text": finished}

        await handle_cancel_command(
            ws,
            ws_id="ws4",
            conn_state={
                "session_id": "s",
                "handler_tasks": handler_tasks,
            },
            surface_mgr=None,
            safe_send_json=send,
        )

        ack = send.await_args.args[1]
        assert "text" not in ack["cancelled"]
        # Slot left as-is (caller's lifecycle responsibility)
        assert handler_tasks["text"] is finished

    @pytest.mark.asyncio
    async def test_task_raising_during_await_does_not_propagate(self):
        """A task that raises a non-CancelledError mid-cancel must
        be swallowed — propagating would tear down the WS read
        loop (preserved verbatim from pre-extract)."""
        ws = _make_ws()
        send = _make_safe_send_json()

        async def _raises_after_cancel():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise RuntimeError("boom on cancel") from None

        bad = asyncio.create_task(_raises_after_cancel())
        # Let it start
        await asyncio.sleep(0)
        handler_tasks = {"text": bad}

        # Must NOT raise.
        await handle_cancel_command(
            ws,
            ws_id="ws5",
            conn_state={
                "session_id": "s",
                "handler_tasks": handler_tasks,
            },
            surface_mgr=None,
            safe_send_json=send,
        )

        ack = send.await_args.args[1]
        assert "text" in ack["cancelled"]
        assert handler_tasks["text"] is None


# ─── Pipeline cancel ──────────────────────────────────────────


class TestPipelineCancel:
    @pytest.mark.asyncio
    async def test_pipeline_cancel_invoked_when_attached(self):
        ws = _make_ws()
        send = _make_safe_send_json()
        pipeline = _make_pipeline()

        await handle_cancel_command(
            ws,
            ws_id="ws6",
            conn_state={"session_id": "s", "pipeline": pipeline},
            surface_mgr=None,
            safe_send_json=send,
        )

        pipeline.cancel.assert_awaited_once()
        ack = send.await_args.args[1]
        assert "pipeline" in ack["cancelled"]

    @pytest.mark.asyncio
    async def test_pipeline_cancel_skipped_when_not_attached(self):
        """Boot race: cancel arrives before register set up the
        pipeline.  Must not crash."""
        ws = _make_ws()
        send = _make_safe_send_json()

        await handle_cancel_command(
            ws,
            ws_id="ws7",
            conn_state={"session_id": "s"},
            surface_mgr=None,
            safe_send_json=send,
        )

        ack = send.await_args.args[1]
        assert "pipeline" not in ack["cancelled"]


# ─── Deferred-widget discard (audit B1 / #165) ───────────────


class TestDeferredWidgetDiscard:
    @pytest.mark.asyncio
    async def test_discard_invoked_when_surface_mgr_and_session(self):
        ws = _make_ws()
        send = _make_safe_send_json()
        surface_mgr = MagicMock()
        surface_mgr.discard_deferred = MagicMock(return_value=2)

        await handle_cancel_command(
            ws,
            ws_id="ws8",
            conn_state={"session_id": "s-active"},
            surface_mgr=surface_mgr,
            safe_send_json=send,
        )

        surface_mgr.discard_deferred.assert_called_once_with("s-active")
        ack = send.await_args.args[1]
        # cancelled list reports the count via "deferred:N"
        assert "deferred:2" in ack["cancelled"]

    @pytest.mark.asyncio
    async def test_zero_dropped_omits_from_ack(self):
        """When discard_deferred returns 0, the ack list must NOT
        contain a `deferred:0` entry (preserved verbatim — keeps
        the ack list focused on what actually got dropped)."""
        ws = _make_ws()
        send = _make_safe_send_json()
        surface_mgr = MagicMock()
        surface_mgr.discard_deferred = MagicMock(return_value=0)

        await handle_cancel_command(
            ws,
            ws_id="ws9",
            conn_state={"session_id": "s"},
            surface_mgr=surface_mgr,
            safe_send_json=send,
        )

        ack = send.await_args.args[1]
        assert not any(c.startswith("deferred:") for c in ack["cancelled"])

    @pytest.mark.asyncio
    async def test_no_surface_mgr_skips_discard(self):
        ws = _make_ws()
        send = _make_safe_send_json()

        await handle_cancel_command(
            ws,
            ws_id="ws10",
            conn_state={"session_id": "s"},
            surface_mgr=None,
            safe_send_json=send,
        )

        ack = send.await_args.args[1]
        assert not any(c.startswith("deferred:") for c in ack["cancelled"])

    @pytest.mark.asyncio
    async def test_no_session_id_skips_discard(self):
        """Cancel arrived before register completed — no session
        to scope the discard against.  Must skip silently."""
        ws = _make_ws()
        send = _make_safe_send_json()
        surface_mgr = MagicMock()
        surface_mgr.discard_deferred = MagicMock()

        await handle_cancel_command(
            ws,
            ws_id="ws11",
            conn_state={},  # no session_id
            surface_mgr=surface_mgr,
            safe_send_json=send,
        )

        surface_mgr.discard_deferred.assert_not_called()


# ─── cancel_ack always sent ──────────────────────────────────


class TestCancelAck:
    @pytest.mark.asyncio
    async def test_full_breakdown_in_ack(self):
        """End-to-end: tasks + pipeline + deferred all reported
        in the same `cancelled` list."""
        ws = _make_ws()
        send = _make_safe_send_json()
        text_t = _make_inflight_task()
        pipeline = _make_pipeline()
        surface_mgr = MagicMock()
        surface_mgr.discard_deferred = MagicMock(return_value=3)

        await handle_cancel_command(
            ws,
            ws_id="ws12",
            conn_state={
                "session_id": "s",
                "pipeline": pipeline,
                "handler_tasks": {"text": text_t},
            },
            surface_mgr=surface_mgr,
            safe_send_json=send,
        )

        ack = send.await_args.args[1]
        assert ack["type"] == "cancel_ack"
        assert "text" in ack["cancelled"]
        assert "pipeline" in ack["cancelled"]
        assert "deferred:3" in ack["cancelled"]
