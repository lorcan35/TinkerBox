"""Tests for ``dragon_voice.clear_handler``.

Pin every branch + the #56 single-dict create_session contract.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.clear_handler import handle_clear_command


def _make_ws(*, closed: bool = False) -> MagicMock:
    ws = MagicMock()
    ws.closed = closed
    ws.send_json = AsyncMock()
    return ws


def _make_pipeline() -> MagicMock:
    p = MagicMock()
    p.clear_history = MagicMock()
    return p


def _make_session_mgr(*, new_sid: str = "new-session-id") -> MagicMock:
    mgr = MagicMock()
    mgr.end_session = AsyncMock()
    mgr.create_session = AsyncMock(return_value={"id": new_sid})
    return mgr


# ─── Pipeline clear ──────────────────────────────────────────


class TestPipelineClear:
    @pytest.mark.asyncio
    async def test_pipeline_clear_history_invoked_when_attached(self):
        ws = _make_ws()
        pipeline = _make_pipeline()
        session_mgr = _make_session_mgr()

        await handle_clear_command(
            ws,
            ws_id="ws1",
            conn_state={
                "pipeline": pipeline,
                "session_id": "old-sid",
                "device_id": "dev1",
            },
            session_mgr=session_mgr,
        )

        pipeline.clear_history.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_pipeline_skips_clear(self):
        """Boot race: clear arrives before register attached
        the pipeline.  Must not crash."""
        ws = _make_ws()
        session_mgr = _make_session_mgr()

        await handle_clear_command(
            ws,
            ws_id="ws2",
            conn_state={
                "session_id": "old-sid",
                "device_id": "dev2",
            },
            session_mgr=session_mgr,
        )
        # No assertion — just must not raise.


# ─── DB session swap ─────────────────────────────────────────


class TestSessionSwap:
    @pytest.mark.asyncio
    async def test_full_swap_emits_session_start(self):
        ws = _make_ws()
        pipeline = _make_pipeline()
        session_mgr = _make_session_mgr(new_sid="fresh-123")
        conn_state = {
            "pipeline": pipeline,
            "session_id": "old-sid",
            "device_id": "tab5-A",
        }

        await handle_clear_command(
            ws,
            ws_id="ws3",
            conn_state=conn_state,
            session_mgr=session_mgr,
        )

        # Old session ended
        session_mgr.end_session.assert_awaited_once_with("old-sid")
        # New session created with the right kwargs
        session_mgr.create_session.assert_awaited_once_with(
            device_id="tab5-A", session_type="conversation",
        )
        # conn_state updated to the new session id
        assert conn_state["session_id"] == "fresh-123"
        # session_start frame emitted with the right shape
        ws.send_json.assert_awaited_once()
        frame = ws.send_json.await_args.args[0]
        assert frame["type"] == "session_start"
        assert frame["session_id"] == "fresh-123"
        assert frame["device_id"] == "tab5-A"
        assert frame["resumed"] is False
        assert frame["message_count"] == 0

    @pytest.mark.asyncio
    async def test_no_session_mgr_skips_db_swap(self):
        """Test path / boot race — no session manager available.
        Pipeline clear still runs; DB swap silently skipped."""
        ws = _make_ws()
        pipeline = _make_pipeline()

        await handle_clear_command(
            ws,
            ws_id="ws4",
            conn_state={
                "pipeline": pipeline,
                "session_id": "old",
                "device_id": "d",
            },
            session_mgr=None,
        )

        pipeline.clear_history.assert_called_once()
        # No session_start emitted (no swap to announce)
        ws.send_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_old_session_id_skips_db_swap(self):
        """Pre-register clear (no session yet) — pipeline clear
        still runs; DB swap silently skipped."""
        ws = _make_ws()
        pipeline = _make_pipeline()
        session_mgr = _make_session_mgr()

        await handle_clear_command(
            ws,
            ws_id="ws5",
            conn_state={
                "pipeline": pipeline,
                # no session_id
                "device_id": "d",
            },
            session_mgr=session_mgr,
        )

        session_mgr.end_session.assert_not_awaited()
        session_mgr.create_session.assert_not_awaited()
        ws.send_json.assert_not_awaited()


# ─── #56 closure pin: single-dict create_session ─────────────


class TestSingleDictCreateSessionContract:
    @pytest.mark.asyncio
    async def test_single_dict_return_shape(self):
        """#56 closure: create_session returns a single dict with
        an "id" key.  NOT a (dict, bool) tuple — that's
        get_or_create_session.  The old tuple-unpack raised
        ValueError and tore down the WS handler.  Pin the
        contract so a future refactor can't drift."""
        ws = _make_ws()
        session_mgr = _make_session_mgr(new_sid="contract-id")

        await handle_clear_command(
            ws,
            ws_id="ws6",
            conn_state={"session_id": "old", "device_id": "d"},
            session_mgr=session_mgr,
        )

        # If create_session returned a tuple, the [\"id\"] index
        # would have raised KeyError on the tuple — meaning the
        # session_start frame would never have been emitted.
        ws.send_json.assert_awaited_once()
        assert ws.send_json.await_args.args[0]["session_id"] == "contract-id"


# ─── ws.closed skip ──────────────────────────────────────────


class TestWsClosedSkip:
    @pytest.mark.asyncio
    async def test_closed_ws_skips_session_start_emit(self):
        """ws closed mid-handler — DB swap still runs (per
        pre-extract behaviour: the swap is a fact-of-life that
        must persist regardless of the WS state) but session_start
        frame is skipped."""
        ws = _make_ws(closed=True)
        session_mgr = _make_session_mgr()

        await handle_clear_command(
            ws,
            ws_id="ws7",
            conn_state={"session_id": "old", "device_id": "d"},
            session_mgr=session_mgr,
        )

        # DB swap still happened
        session_mgr.end_session.assert_awaited_once()
        session_mgr.create_session.assert_awaited_once()
        # But no frame emitted
        ws.send_json.assert_not_awaited()
