"""Tests for ``dragon_voice.pipeline_callbacks``.

Pin every callback branch + the v4·D audit P0 fix that
disconnects mid-stream don't raise into the pipeline loop.
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.pipeline_callbacks import PipelineCallbacks


@dataclass
class _StubBillingConfig:
    """Minimal stand-in for BillingConfig that the W5-B cap-check
    path reads.  Real config dataclass lives in dragon_voice.config."""
    daily_cap_cents: int = 0


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


# ─── W5-B: daily cap trigger ─────────────────────────────────


class TestDailyCapTrigger:
    """Verify that PipelineCallbacks emits a one-shot `cap_downgrade`
    frame when today's spend exceeds `billing_config.daily_cap_cents`.

    Strategy: patch `summarize_spend_for_day` to return a known
    SpendSummary so we can control whether the cap is breached
    without setting up a real events table."""

    @pytest.mark.asyncio
    async def test_no_cap_when_cents_is_zero(self, monkeypatch):
        """daily_cap_cents=0 → cap-check is a no-op even if spend is huge."""
        send_json = _make_safe_send_json()
        db = _make_db()
        cb = PipelineCallbacks(
            _make_ws(),
            conn_state={"session_id": "s", "device_id": "d"},
            safe_send_bytes=_make_safe_send_bytes(),
            safe_send_json=send_json,
            db=db,
            billing_config=_StubBillingConfig(daily_cap_cents=0),
        )

        from dragon_voice.billing import spend_tracker as st

        # If cap=0, summarize_spend_for_day should never even be called.
        # Patch it to raise so we'd detect a regression.
        async def _should_not_be_called(*a, **k):
            raise AssertionError("summarize_spend_for_day called with cap=0")
        monkeypatch.setattr(st, "summarize_spend_for_day", _should_not_be_called)

        await cb.on_event({"type": "api_usage", "cost_mils": 9999})

        # Only the api_usage emit was sent — no cap_downgrade.
        types_sent = [c.args[1].get("type") for c in send_json.await_args_list]
        assert types_sent == ["api_usage"]

    @pytest.mark.asyncio
    async def test_under_cap_no_emit(self, monkeypatch):
        send_json = _make_safe_send_json()
        cb = PipelineCallbacks(
            _make_ws(),
            conn_state={"session_id": "s", "device_id": "d"},
            safe_send_bytes=_make_safe_send_bytes(),
            safe_send_json=send_json,
            db=_make_db(),
            billing_config=_StubBillingConfig(daily_cap_cents=100),
        )

        # Mock the summary to report 50c spent — under the 100c cap.
        from dragon_voice.billing import spend_tracker as st
        from dragon_voice.billing.spend_tracker import SpendSummary

        async def _fake(*a, **k):
            return SpendSummary(day="2026-05-12", total_mils=50_000, event_count=5)
        monkeypatch.setattr(st, "summarize_spend_for_day", _fake)

        await cb.on_event({"type": "api_usage", "cost_mils": 10})

        types_sent = [c.args[1].get("type") for c in send_json.await_args_list]
        assert "cap_downgrade" not in types_sent

    @pytest.mark.asyncio
    async def test_over_cap_emits_once(self, monkeypatch):
        send_json = _make_safe_send_json()
        cb = PipelineCallbacks(
            _make_ws(),
            conn_state={"session_id": "s", "device_id": "d", "turn_id": "abc123"},
            safe_send_bytes=_make_safe_send_bytes(),
            safe_send_json=send_json,
            db=_make_db(),
            billing_config=_StubBillingConfig(daily_cap_cents=100),
        )

        from dragon_voice.billing import spend_tracker as st
        from dragon_voice.billing.spend_tracker import SpendSummary

        async def _fake(*a, **k):
            return SpendSummary(day="2026-05-12", total_mils=150_000, event_count=10)
        monkeypatch.setattr(st, "summarize_spend_for_day", _fake)
        monkeypatch.setattr(st, "today_iso", lambda: "2026-05-12")

        # First api_usage event — should trigger the alert.
        await cb.on_event({"type": "api_usage", "cost_mils": 50})

        # Second event — same day, cap already alerted, should NOT re-emit.
        await cb.on_event({"type": "api_usage", "cost_mils": 20})

        # Count cap_downgrade frames sent.
        sent_types = [c.args[1].get("type") for c in send_json.await_args_list]
        assert sent_types.count("cap_downgrade") == 1

        # Verify the alert frame shape.
        cap_frame = next(c.args[1] for c in send_json.await_args_list
                         if c.args[1].get("type") == "cap_downgrade")
        assert cap_frame["reason"] == "daily_cap_hit"
        assert cap_frame["spent_cents"] == 150
        assert cap_frame["cap_cents"] == 100
        assert cap_frame["day"] == "2026-05-12"
        assert cap_frame["turn_id"] == "abc123"

    @pytest.mark.asyncio
    async def test_no_cap_check_when_billing_config_is_none(self, monkeypatch):
        """Backward-compat: callers that don't pass billing_config get
        the old behaviour (no cap trigger)."""
        send_json = _make_safe_send_json()
        cb = PipelineCallbacks(
            _make_ws(),
            conn_state={"session_id": "s", "device_id": "d"},
            safe_send_bytes=_make_safe_send_bytes(),
            safe_send_json=send_json,
            db=_make_db(),
            # No billing_config — defaults to None.
        )

        from dragon_voice.billing import spend_tracker as st

        async def _should_not_be_called(*a, **k):
            raise AssertionError("summarize_spend_for_day called with no billing_config")
        monkeypatch.setattr(st, "summarize_spend_for_day", _should_not_be_called)

        await cb.on_event({"type": "api_usage", "cost_mils": 9999})
        sent_types = [c.args[1].get("type") for c in send_json.await_args_list]
        assert "cap_downgrade" not in sent_types

    @pytest.mark.asyncio
    async def test_spend_tracker_failure_swallowed(self, monkeypatch):
        """If summarize_spend_for_day raises, the turn must NOT
        fail — cap-check is best-effort observability."""
        send_json = _make_safe_send_json()
        cb = PipelineCallbacks(
            _make_ws(),
            conn_state={"session_id": "s", "device_id": "d"},
            safe_send_bytes=_make_safe_send_bytes(),
            safe_send_json=send_json,
            db=_make_db(),
            billing_config=_StubBillingConfig(daily_cap_cents=100),
        )

        from dragon_voice.billing import spend_tracker as st

        async def _boom(*a, **k):
            raise RuntimeError("db connection dead")
        monkeypatch.setattr(st, "summarize_spend_for_day", _boom)

        # Should NOT raise even though the cap-check internally errors.
        await cb.on_event({"type": "api_usage", "cost_mils": 50})
        # api_usage still emitted.
        sent_types = [c.args[1].get("type") for c in send_json.await_args_list]
        assert sent_types == ["api_usage"]
