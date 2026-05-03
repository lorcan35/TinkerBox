"""Tests for ``dragon_voice.config_update_rate_limit``.

Pin every branch + the audit C1 (#137) closure that the
rate-limit gate emits a γ-arch event (not silent debug).
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dragon_voice.config_update_rate_limit import (
    _DEFAULT_MIN_INTERVAL_S,
    _LAST_TS_KEY,
    check_config_update_rate_limit,
)
from dragon_voice.errors import Scope, Severity


def _make_ws(*, closed: bool = False) -> MagicMock:
    ws = MagicMock()
    ws.closed = closed
    return ws


def _make_safe_send_json() -> AsyncMock:
    return AsyncMock(return_value=True)


# ─── Allowed branch ──────────────────────────────────────────


class TestAllowedBranch:
    @pytest.mark.asyncio
    async def test_first_ever_call_allowed_and_stamps_timestamp(self):
        ws = _make_ws()
        send = _make_safe_send_json()
        conn_state: dict = {}

        result = await check_config_update_rate_limit(
            ws, conn_state=conn_state, ws_id="ws1",
            safe_send_json=send,
        )

        assert result is True
        assert _LAST_TS_KEY in conn_state
        assert conn_state[_LAST_TS_KEY] > 0
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_call_after_interval_allowed_and_updates_timestamp(self):
        ws = _make_ws()
        send = _make_safe_send_json()
        # Pre-stamp with a timestamp from "long enough ago"
        conn_state: dict = {_LAST_TS_KEY: time.monotonic() - 5.0}
        old_ts = conn_state[_LAST_TS_KEY]

        result = await check_config_update_rate_limit(
            ws, conn_state=conn_state, ws_id="ws2",
            safe_send_json=send,
        )

        assert result is True
        assert conn_state[_LAST_TS_KEY] > old_ts
        send.assert_not_awaited()


# ─── Rate-limited branch ─────────────────────────────────────


class TestRateLimitedBranch:
    @pytest.mark.asyncio
    async def test_too_soon_returns_false_emits_error(self):
        ws = _make_ws()
        send = _make_safe_send_json()
        # Pre-stamp with a timestamp from "just now"
        conn_state: dict = {_LAST_TS_KEY: time.monotonic()}
        old_ts = conn_state[_LAST_TS_KEY]

        result = await check_config_update_rate_limit(
            ws, conn_state=conn_state, ws_id="ws3",
            safe_send_json=send,
        )

        assert result is False
        # Timestamp NOT updated (so the original cooldown stays)
        assert conn_state[_LAST_TS_KEY] == old_ts

        # γ-arch event emitted — audit C1 (#137) closure
        send.assert_awaited_once()
        frame = send.await_args.args[1]
        assert frame["code"] == "config_update_rate_limited"
        assert frame["severity"] == Severity.TRANSIENT.value
        assert frame["scope"] == Scope.SESSION.value
        assert "rate-limited" in frame["message"]

    @pytest.mark.asyncio
    async def test_rate_limited_with_closed_ws_skips_emit_but_still_returns_false(self):
        """Disconnected client: no point emitting, but the gate
        still fires so the caller short-circuits."""
        ws = _make_ws(closed=True)
        send = _make_safe_send_json()
        conn_state: dict = {_LAST_TS_KEY: time.monotonic()}

        result = await check_config_update_rate_limit(
            ws, conn_state=conn_state, ws_id="ws4",
            safe_send_json=send,
        )

        assert result is False
        send.assert_not_awaited()


# ─── Custom interval parameter ───────────────────────────────


class TestCustomInterval:
    @pytest.mark.asyncio
    async def test_custom_interval_overrides_default(self):
        """A future config knob can pass a different
        min_interval_s — pin the parameter wiring."""
        ws = _make_ws()
        send = _make_safe_send_json()
        # Stamp 0.3 s ago — under default 0.5, over a custom 0.1
        conn_state: dict = {_LAST_TS_KEY: time.monotonic() - 0.3}

        # Default: blocked
        result_default = await check_config_update_rate_limit(
            ws, conn_state=conn_state, ws_id="ws5",
            safe_send_json=send,
        )
        assert result_default is False

        # Custom 0.1 s: allowed
        # (Reset timestamp because the previous call ran but
        # didn't update on rate-limit)
        conn_state[_LAST_TS_KEY] = time.monotonic() - 0.3
        result_custom = await check_config_update_rate_limit(
            ws, conn_state=conn_state, ws_id="ws5",
            safe_send_json=_make_safe_send_json(),
            min_interval_s=0.1,
        )
        assert result_custom is True


# ─── Default constant pin ────────────────────────────────────


class TestDefaultInterval:
    def test_default_min_interval_is_500ms(self):
        """Pin the 0.5 s default — matches pre-extract behaviour
        and the 2-per-second tolerance v4·D audit P1 set."""
        assert _DEFAULT_MIN_INTERVAL_S == 0.5

    def test_last_ts_key_constant(self):
        """conn_state key name is part of the call-site contract
        — extracted modules and tests reference it directly."""
        assert _LAST_TS_KEY == "_last_config_update_ts"


# ─── Audit C1 (#137) closure pin ─────────────────────────────


class TestAuditC1Closure:
    @pytest.mark.asyncio
    async def test_emits_gamma_arch_event_not_silent_debug(self):
        """Pre-fix the rate-limit was a silent `logger.debug` +
        `return` — Tab5's mode-toggle UI sat on its previous
        local state and the user assumed the swap landed.  Pin
        that we emit the γ-arch event so Tab5 can render a toast."""
        ws = _make_ws()
        send = _make_safe_send_json()
        conn_state: dict = {_LAST_TS_KEY: time.monotonic()}

        await check_config_update_rate_limit(
            ws, conn_state=conn_state, ws_id="audit-c1",
            safe_send_json=send,
        )

        # MUST emit something (not silently swallow).
        send.assert_awaited_once()
        # MUST be the structured γ-arch event (not a freeform "error"
        # or "info" payload).
        frame = send.await_args.args[1]
        assert frame.get("type") == "error"
        assert frame.get("code") == "config_update_rate_limited"


# ─── Realistic timing test ───────────────────────────────────


class TestRealisticTiming:
    @pytest.mark.asyncio
    async def test_two_quick_calls_then_wait_then_third_succeeds(self):
        """Two rapid calls: first allowed, second blocked, then
        wait 0.6 s and third succeeds."""
        ws = _make_ws()
        send_1 = _make_safe_send_json()
        conn_state: dict = {}

        # First — allowed
        r1 = await check_config_update_rate_limit(
            ws, conn_state=conn_state, ws_id="ws6",
            safe_send_json=send_1,
        )
        assert r1 is True

        # Second immediately — blocked
        send_2 = _make_safe_send_json()
        r2 = await check_config_update_rate_limit(
            ws, conn_state=conn_state, ws_id="ws6",
            safe_send_json=send_2,
        )
        assert r2 is False
        send_2.assert_awaited_once()

        # Simulate 0.6 s passing by rewinding the timestamp
        conn_state[_LAST_TS_KEY] -= 1.0

        # Third — allowed again
        send_3 = _make_safe_send_json()
        r3 = await check_config_update_rate_limit(
            ws, conn_state=conn_state, ws_id="ws6",
            safe_send_json=send_3,
        )
        assert r3 is True
        send_3.assert_not_awaited()
