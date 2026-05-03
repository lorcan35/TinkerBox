"""Tests for ``dragon_voice.ws_keepalive``.

Pin every loop-exit branch and the failure-counter reset
behaviour so a future refactor can't drift on:

  * Stop-event-set early exit (before sleep, after sleep)
  * ws.closed early exit (before sleep, after sleep)
  * Successful send → fail_count resets
  * Timeout under threshold → continue
  * 3 timeouts → close + return
  * Send-exception under threshold → continue
  * 3 send-exceptions → close + return
  * Successful send AFTER timeouts resets the counter
  * Tiny interval used in tests so we don't sit in real sleeps
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.ws_keepalive import run_ws_keepalive


def _make_ws(*, closed: bool = False) -> MagicMock:
    ws = MagicMock()
    ws.closed = closed
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()
    return ws


# ─── Stop-event branches ───────────────────────────────────────


class TestStopEvent:
    @pytest.mark.asyncio
    async def test_stop_event_set_before_first_iteration_returns_immediately(self):
        ws = _make_ws()
        stop = asyncio.Event()
        stop.set()

        await run_ws_keepalive(
            ws, ws_id="ws1", stop_event=stop,
            interval_s=0.001,
        )

        ws.send_json.assert_not_awaited()
        ws.close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_event_set_during_sleep_returns_after_sleep(self):
        """Caller sets stop_event while we're sleeping; we should
        notice on wake and return without sending another ping."""
        ws = _make_ws()
        stop = asyncio.Event()

        async def _set_stop_after_short_delay():
            await asyncio.sleep(0.005)
            stop.set()

        # Start the stopper concurrently — stash the ref to keep
        # the task alive past this scope (RUF006).
        _stopper = asyncio.create_task(_set_stop_after_short_delay())

        await run_ws_keepalive(
            ws, ws_id="ws2", stop_event=stop,
            interval_s=0.02,  # > stopper delay
        )

        # We may have sent zero pings (stop fired before first
        # interval elapsed); we definitely shouldn't have closed.
        ws.close.assert_not_awaited()
        _ = _stopper  # silence unused; the ref itself is the point


# ─── ws.closed branches ────────────────────────────────────────


class TestWsClosed:
    @pytest.mark.asyncio
    async def test_ws_closed_at_start_returns_immediately(self):
        ws = _make_ws(closed=True)
        await run_ws_keepalive(
            ws, ws_id="ws3", stop_event=asyncio.Event(),
            interval_s=0.001,
        )
        ws.send_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ws_closes_during_sleep(self):
        """ws.closed flips to True mid-sleep; on wake we notice
        and return without sending."""
        ws = _make_ws(closed=False)

        async def _close_ws():
            await asyncio.sleep(0.005)
            ws.closed = True

        _closer = asyncio.create_task(_close_ws())  # noqa: F841 (RUF006: keep ref alive)

        await run_ws_keepalive(
            ws, ws_id="ws4",
            interval_s=0.02,
        )
        _ = _closer  # silence unused; the reference itself is the point

        ws.send_json.assert_not_awaited()


# ─── Happy path ───────────────────────────────────────────────


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_sends_ping_then_continues(self):
        """One successful ping, then close ws to break the loop."""
        ws = _make_ws()
        send_count = 0

        async def _send_then_close(payload):
            nonlocal send_count
            send_count += 1
            if send_count >= 2:
                ws.closed = True

        ws.send_json.side_effect = _send_then_close

        await run_ws_keepalive(
            ws, ws_id="ws5",
            interval_s=0.001,
        )

        # 2 pings sent (the second one set ws.closed; the next
        # iteration's `if ws.closed: return` exits cleanly).
        assert ws.send_json.await_count == 2
        for c in ws.send_json.await_args_list:
            assert c.args[0] == {"type": "pong"}
        # Clean exit — no close() call (only failure-threshold
        # path closes the WS from inside the loop).
        ws.close.assert_not_awaited()


# ─── Timeout branch ───────────────────────────────────────────


class TestTimeoutFailures:
    @pytest.mark.asyncio
    async def test_one_timeout_increments_then_continues(self):
        """One timeout under threshold → continue, don't close."""
        ws = _make_ws()
        call_count = 0

        async def _send(payload):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Hang long enough to trigger the wait_for timeout
                await asyncio.sleep(10)
            elif call_count == 2:
                # Second call: succeed and close ws to exit
                ws.closed = True

        ws.send_json.side_effect = _send

        await run_ws_keepalive(
            ws, ws_id="ws6",
            interval_s=0.001,
            send_timeout_s=0.005,
            max_consecutive_failures=3,
        )

        # 2 send attempts — first timed out, second succeeded
        assert ws.send_json.await_count == 2
        # No close — failure counter reset to 0 after the second
        ws.close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_three_consecutive_timeouts_close_ws(self):
        """3 timeouts in a row → close + return."""
        ws = _make_ws()

        async def _hang(payload):
            await asyncio.sleep(10)  # will time out

        ws.send_json.side_effect = _hang

        await run_ws_keepalive(
            ws, ws_id="ws7",
            interval_s=0.001,
            send_timeout_s=0.005,
            max_consecutive_failures=3,
        )

        # Exactly 3 timeouts fired the close.
        assert ws.send_json.await_count == 3
        ws.close.assert_awaited_once()


# ─── Send-exception branch ────────────────────────────────────


class TestSendExceptionFailures:
    @pytest.mark.asyncio
    async def test_one_exception_increments_then_continues(self):
        ws = _make_ws()
        call_count = 0

        async def _send(payload):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionResetError("peer gone")
            elif call_count == 2:
                ws.closed = True  # exit cleanly

        ws.send_json.side_effect = _send

        await run_ws_keepalive(
            ws, ws_id="ws8",
            interval_s=0.001,
            max_consecutive_failures=3,
        )

        assert ws.send_json.await_count == 2
        ws.close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_three_consecutive_exceptions_close_ws(self):
        ws = _make_ws()
        ws.send_json.side_effect = ConnectionResetError("dead")

        await run_ws_keepalive(
            ws, ws_id="ws9",
            interval_s=0.001,
            max_consecutive_failures=3,
        )

        assert ws.send_json.await_count == 3
        ws.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_failure_does_not_propagate(self):
        """When ws.close() itself raises, we still return cleanly
        (the WS is dead anyway; we don't want the keepalive task
        to crash and leak the connection record)."""
        ws = _make_ws()
        ws.send_json.side_effect = RuntimeError("dead")
        ws.close.side_effect = RuntimeError("close also broken")

        # Must not raise.
        await run_ws_keepalive(
            ws, ws_id="ws10",
            interval_s=0.001,
            max_consecutive_failures=2,
        )


# ─── Counter-reset across mixed failures ──────────────────────


class TestCounterResetOnSuccess:
    @pytest.mark.asyncio
    async def test_success_after_two_failures_resets_counter(self):
        """Sequence: timeout, timeout, success, exception, exception,
        exception → only the last 3-in-a-row exception streak fires
        the close.  This pins the consecutive-failures semantic
        (not a cumulative counter)."""
        ws = _make_ws()
        seq = iter([
            "timeout", "timeout", "success",
            "exc", "exc", "exc",
        ])

        async def _send(payload):
            kind = next(seq)
            if kind == "timeout":
                await asyncio.sleep(10)
            elif kind == "success":
                return  # success
            elif kind == "exc":
                raise RuntimeError("boom")

        ws.send_json.side_effect = _send

        await run_ws_keepalive(
            ws, ws_id="ws11",
            interval_s=0.001,
            send_timeout_s=0.005,
            max_consecutive_failures=3,
        )

        # 6 send attempts: 2 timeouts + 1 success + 3 exceptions
        assert ws.send_json.await_count == 6
        # Close fired by the 3-exception streak (success reset
        # after the 2 timeouts so the streak count is per-tail).
        ws.close.assert_awaited_once()


# ─── Default tuning constants ─────────────────────────────────


class TestDefaultTuning:
    def test_defaults_match_pre_extract_constants(self):
        """Pin the default tuning so a future refactor can't
        accidentally regress the 15s/5s/3 trio that ngrok-vs-LLM
        latency was tuned around (#75 + US-DQ05 + US-DQ20)."""
        from dragon_voice.ws_keepalive import (
            _DEFAULT_INTERVAL_S,
            _DEFAULT_MAX_FAILURES,
            _DEFAULT_SEND_TIMEOUT_S,
        )
        assert _DEFAULT_INTERVAL_S == 15.0
        assert _DEFAULT_SEND_TIMEOUT_S == 5.0
        assert _DEFAULT_MAX_FAILURES == 3
