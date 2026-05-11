"""Tests for ``dragon_voice.pipeline_callbacks``.

Pin every callback branch + the v4·D audit P0 fix that
disconnects mid-stream don't raise into the pipeline loop.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.pipeline_callbacks import PipelineCallbacks


def _make_ws(*, closed: bool = False) -> MagicMock:
    ws = MagicMock()
    ws.closed = closed
    return ws


def _make_safe_send_bytes() -> AsyncMock:
    return AsyncMock(return_value=True)


def _make_safe_send_json() -> AsyncMock:
    return AsyncMock(return_value=True)


def _make_db() -> MagicMock:
    db = MagicMock()
    db.add_event = AsyncMock()
    return db


def _make_callbacks(**overrides) -> PipelineCallbacks:
    defaults = {
        "ws": _make_ws(),
        "conn_state": {"session_id": "s", "device_id": "d"},
        "safe_send_bytes": _make_safe_send_bytes(),
        "safe_send_json": _make_safe_send_json(),
        "db": _make_db(),
    }
    defaults.update(overrides)
    ws = defaults.pop("ws")
    return PipelineCallbacks(ws=ws, **defaults)


# ─── on_audio ────────────────────────────────────────────────


class TestOnAudio:
    @pytest.mark.asyncio
    async def test_open_ws_routes_through_safe_send_bytes(self):
        ws = _make_ws()
        send_bytes = _make_safe_send_bytes()
        cb = PipelineCallbacks(
            ws,
            conn_state={},
            safe_send_bytes=send_bytes,
            safe_send_json=_make_safe_send_json(),
            db=None,
        )

        payload = b"\x01\x02\x03\x04"
        await cb.on_audio(payload)

        send_bytes.assert_awaited_once_with(ws, payload)

    @pytest.mark.asyncio
    async def test_closed_ws_skips_send(self):
        """v4·D audit P0 fix: closed-ws check before routing
        through safe_send_bytes prevents a redundant call."""
        ws = _make_ws(closed=True)
        send_bytes = _make_safe_send_bytes()
        cb = PipelineCallbacks(
            ws,
            conn_state={},
            safe_send_bytes=send_bytes,
            safe_send_json=_make_safe_send_json(),
            db=None,
        )

        await cb.on_audio(b"data")

        send_bytes.assert_not_awaited()


# ─── on_event ────────────────────────────────────────────────


class TestOnEvent:
    @pytest.mark.asyncio
    async def test_event_forwarded_to_safe_send_json(self):
        ws = _make_ws()
        send_json = _make_safe_send_json()
        cb = PipelineCallbacks(
            ws,
            conn_state={"session_id": "s", "device_id": "d"},
            safe_send_bytes=_make_safe_send_bytes(),
            safe_send_json=send_json,
            db=None,
        )

        event = {"type": "state", "stage": "thinking"}
        await cb.on_event(event)

        send_json.assert_awaited_once_with(ws, event)

    @pytest.mark.asyncio
    async def test_turn_id_stamped_from_conn_state(self):
        """W4-C: every Dragon→Tab5 emit picks up `turn_id` from
        conn_state.  Single chokepoint for cross-system trace
        correlation."""
        ws = _make_ws()
        send_json = _make_safe_send_json()
        cb = PipelineCallbacks(
            ws,
            conn_state={"session_id": "s", "device_id": "d", "turn_id": "abc123def456"},
            safe_send_bytes=_make_safe_send_bytes(),
            safe_send_json=send_json,
            db=None,
        )
        await cb.on_event({"type": "llm_done", "llm_ms": 123})

        # Frame forwarded to ws includes turn_id from conn_state.
        forwarded = send_json.await_args.args[1]
        assert forwarded["turn_id"] == "abc123def456"
        assert forwarded["type"] == "llm_done"
        assert forwarded["llm_ms"] == 123

    @pytest.mark.asyncio
    async def test_turn_id_default_when_conn_state_missing_field(self):
        """Pre-W4-A firmwares + warm-boot before first turn → conn_state
        has no turn_id → fall back to "-" so logs/frames stay
        greppable + structurally consistent."""
        ws = _make_ws()
        send_json = _make_safe_send_json()
        cb = PipelineCallbacks(
            ws,
            conn_state={"session_id": "s", "device_id": "d"},
            safe_send_bytes=_make_safe_send_bytes(),
            safe_send_json=send_json,
            db=None,
        )
        await cb.on_event({"type": "stt", "text": "hello"})
        forwarded = send_json.await_args.args[1]
        assert forwarded["turn_id"] == "-"

    @pytest.mark.asyncio
    async def test_explicit_turn_id_in_event_not_overwritten(self):
        """W4-C respects an explicit turn_id set by the caller — lets
        downstream emit sites override per-event (rare; useful for
        background out-of-band emits like scheduler reminders that
        don't belong to the current foreground turn)."""
        ws = _make_ws()
        send_json = _make_safe_send_json()
        cb = PipelineCallbacks(
            ws,
            conn_state={"session_id": "s", "device_id": "d", "turn_id": "foreground"},
            safe_send_bytes=_make_safe_send_bytes(),
            safe_send_json=send_json,
            db=None,
        )
        await cb.on_event({"type": "scheduler_fire", "turn_id": "background-scheduler-1"})
        forwarded = send_json.await_args.args[1]
        assert forwarded["turn_id"] == "background-scheduler-1"

    @pytest.mark.asyncio
    async def test_closed_ws_skips_send_json_but_still_persists(self):
        """When ws is closed mid-flight, we skip the WS emit but
        STILL persist api_usage to the DB (cost tracking must
        survive disconnect-mid-event)."""
        ws = _make_ws(closed=True)
        send_json = _make_safe_send_json()
        db = _make_db()
        cb = PipelineCallbacks(
            ws,
            conn_state={"session_id": "s", "device_id": "d"},
            safe_send_bytes=_make_safe_send_bytes(),
            safe_send_json=send_json,
            db=db,
        )

        await cb.on_event({"type": "api_usage", "cost_mils": 12})

        send_json.assert_not_awaited()
        db.add_event.assert_awaited_once()


# ─── api_usage DB persistence ────────────────────────────────


class TestApiUsagePersistence:
    @pytest.mark.asyncio
    async def test_api_usage_persisted_with_session_and_device(self):
        ws = _make_ws()
        db = _make_db()
        cb = PipelineCallbacks(
            ws,
            conn_state={"session_id": "sess-A", "device_id": "tab5-7"},
            safe_send_bytes=_make_safe_send_bytes(),
            safe_send_json=_make_safe_send_json(),
            db=db,
        )

        event = {
            "type": "api_usage",
            "model": "anthropic/claude-haiku",
            "tokens": 150,
            "cost_mils": 5,
        }
        await cb.on_event(event)

        db.add_event.assert_awaited_once()
        call_kwargs = db.add_event.await_args.kwargs
        assert db.add_event.await_args.args[0] == "api_usage"
        assert call_kwargs["session_id"] == "sess-A"
        assert call_kwargs["device_id"] == "tab5-7"
        # Data dict has everything except `type` (already in pos arg).
        # W4-C (audit 2026-05-11) stamps `turn_id` from conn_state on every
        # event, so it also lands in the persisted api_usage row — useful
        # for per-turn cost analysis.  conn_state has no turn_id field in
        # this test fixture, so falls back to the "-" default.
        assert call_kwargs["data"] == {
            "model": "anthropic/claude-haiku",
            "tokens": 150,
            "cost_mils": 5,
            "turn_id": "-",
        }

    @pytest.mark.asyncio
    async def test_non_api_usage_event_does_not_persist(self):
        """Pin the "only api_usage hits the DB" semantic — keeps
        the events table focused on cost analysis."""
        ws = _make_ws()
        db = _make_db()
        cb = PipelineCallbacks(
            ws,
            conn_state={"session_id": "s", "device_id": "d"},
            safe_send_bytes=_make_safe_send_bytes(),
            safe_send_json=_make_safe_send_json(),
            db=db,
        )

        await cb.on_event({"type": "state", "stage": "idle"})
        await cb.on_event({"type": "tool_call", "name": "x"})
        await cb.on_event({"type": "llm_done"})

        db.add_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_db_skips_persistence_silently(self):
        """Test path / boot race — no DB available.  WS emit
        still runs; persist silently skipped."""
        ws = _make_ws()
        send_json = _make_safe_send_json()
        cb = PipelineCallbacks(
            ws,
            conn_state={"session_id": "s", "device_id": "d"},
            safe_send_bytes=_make_safe_send_bytes(),
            safe_send_json=send_json,
            db=None,
        )

        await cb.on_event({"type": "api_usage", "cost_mils": 1})

        send_json.assert_awaited_once()  # WS emit still ran
        # No DB call possible (db is None); just must not crash.

    @pytest.mark.asyncio
    async def test_db_failure_does_not_propagate(self):
        """add_event raising must NOT propagate up — that would
        tear down the pipeline's tight event loop."""
        ws = _make_ws()
        db = MagicMock()
        db.add_event = AsyncMock(side_effect=RuntimeError("db down"))
        cb = PipelineCallbacks(
            ws,
            conn_state={"session_id": "s", "device_id": "d"},
            safe_send_bytes=_make_safe_send_bytes(),
            safe_send_json=_make_safe_send_json(),
            db=db,
        )

        # Must NOT raise.
        await cb.on_event({"type": "api_usage", "cost_mils": 1})


# ─── Conn_state read-fresh per call ──────────────────────────


class TestConnStateReadFreshPerCall:
    @pytest.mark.asyncio
    async def test_session_id_change_visible_on_next_event(self):
        """Pin the per-event conn_state lookup so a `clear` cmd
        that swapped the session_id is reflected on the next
        api_usage persist."""
        ws = _make_ws()
        db = _make_db()
        conn_state = {"session_id": "first", "device_id": "d"}
        cb = PipelineCallbacks(
            ws,
            conn_state=conn_state,
            safe_send_bytes=_make_safe_send_bytes(),
            safe_send_json=_make_safe_send_json(),
            db=db,
        )

        # First event — uses "first"
        await cb.on_event({"type": "api_usage", "cost_mils": 1})
        # Mid-stream, simulate a `clear` command swapping the session
        conn_state["session_id"] = "second"
        # Second event — must use "second"
        await cb.on_event({"type": "api_usage", "cost_mils": 2})

        first_call = db.add_event.await_args_list[0]
        second_call = db.add_event.await_args_list[1]
        assert first_call.kwargs["session_id"] == "first"
        assert second_call.kwargs["session_id"] == "second"
