"""Tests for B1 (#165): SurfaceManager TurnGate + scheduler defer
during in-flight turns + cancel discards deferred emits.
"""
from __future__ import annotations

import asyncio

import pytest

from dragon_voice.surfaces.manager import SurfaceManager


def _make_mgr_with_session(session_id: str = "s1") -> tuple[SurfaceManager, list[dict]]:
    """Register a fake session whose `send` callable just appends to a
    captured-list.  Returns (manager, captured)."""
    mgr = SurfaceManager()
    captured: list[dict] = []

    async def _fake_send(msg: dict) -> None:
        captured.append(msg)

    asyncio.run(mgr.register_session(session_id, _fake_send))
    return mgr, captured


# ─────────────────────────── basic TurnGate state


def test_is_turn_busy_false_for_unknown_session() -> None:
    mgr = SurfaceManager()
    assert mgr.is_turn_busy("nope") is False


def test_mark_turn_start_then_end_toggles_busy_flag() -> None:
    mgr, _ = _make_mgr_with_session()
    assert mgr.is_turn_busy("s1") is False
    mgr.mark_turn_start("s1")
    assert mgr.is_turn_busy("s1") is True

    asyncio.run(mgr.mark_turn_end("s1"))
    assert mgr.is_turn_busy("s1") is False


def test_mark_turn_start_is_idempotent() -> None:
    mgr, _ = _make_mgr_with_session()
    mgr.mark_turn_start("s1")
    mgr.mark_turn_start("s1")  # must not raise
    assert mgr.is_turn_busy("s1") is True


def test_mark_turn_for_unknown_session_is_no_op() -> None:
    mgr = SurfaceManager()
    mgr.mark_turn_start("nope")  # must not raise
    asyncio.run(mgr.mark_turn_end("nope"))  # must not raise


# ─────────────────────────── defer_or_send routing


def test_defer_or_send_runs_immediately_when_idle() -> None:
    mgr, _ = _make_mgr_with_session()
    fired = []

    async def _send_fn():
        fired.append("a")

    sent_now = asyncio.run(mgr.defer_or_send("s1", _send_fn))
    assert sent_now is True
    assert fired == ["a"]


def test_defer_or_send_queues_when_busy() -> None:
    mgr, _ = _make_mgr_with_session()
    mgr.mark_turn_start("s1")
    fired = []

    async def _send_fn():
        fired.append("a")

    sent_now = asyncio.run(mgr.defer_or_send("s1", _send_fn))
    assert sent_now is False
    assert fired == [], "send must be deferred while busy"


def test_mark_turn_end_drains_queue_in_fifo_order() -> None:
    mgr, _ = _make_mgr_with_session()
    mgr.mark_turn_start("s1")
    fired = []

    async def _make_sender(label):
        async def _fn():
            fired.append(label)
        return _fn

    async def go():
        for label in ("a", "b", "c"):
            fn = await _make_sender(label)
            await mgr.defer_or_send("s1", fn)
        await mgr.mark_turn_end("s1")

    asyncio.run(go())
    assert fired == ["a", "b", "c"]


def test_mark_turn_end_with_empty_queue_is_no_op() -> None:
    mgr, _ = _make_mgr_with_session()
    mgr.mark_turn_start("s1")
    asyncio.run(mgr.mark_turn_end("s1"))  # must not raise


def test_mark_turn_end_continues_past_raising_send_fn() -> None:
    """One bad deferred emit must not block the rest of the queue."""
    mgr, _ = _make_mgr_with_session()
    mgr.mark_turn_start("s1")
    fired = []

    async def _good():
        fired.append("good")

    async def _bad():
        raise RuntimeError("intentional test failure")

    async def go():
        await mgr.defer_or_send("s1", _bad)
        await mgr.defer_or_send("s1", _good)
        await mgr.mark_turn_end("s1")

    asyncio.run(go())
    assert fired == ["good"], "good emit must still fire after bad emit raised"


def test_defer_or_send_for_unknown_session_runs_immediately() -> None:
    """An emit targeting an unregistered session falls through to
    immediate run (the caller's send_fn is expected to be defensive)."""
    mgr = SurfaceManager()
    fired = []

    async def _send_fn():
        fired.append("a")

    sent_now = asyncio.run(mgr.defer_or_send("ghost-session", _send_fn))
    assert sent_now is True
    assert fired == ["a"]


# ─────────────────────────── discard_deferred (cancel hook)


def test_discard_deferred_drops_pending_emits() -> None:
    """User cancels mid-turn; deferred reminder fires must NOT
    materialise on the WS afterwards."""
    mgr, _ = _make_mgr_with_session()
    mgr.mark_turn_start("s1")
    fired = []

    async def _send_fn():
        fired.append("never")

    async def go():
        await mgr.defer_or_send("s1", _send_fn)
        dropped = mgr.discard_deferred("s1")
        await mgr.mark_turn_end("s1")
        return dropped

    dropped = asyncio.run(go())
    assert dropped == 1, "discard_deferred should report the drop count"
    assert fired == [], "discarded send must never run"


def test_discard_deferred_with_empty_queue_returns_zero() -> None:
    mgr, _ = _make_mgr_with_session()
    mgr.mark_turn_start("s1")
    assert mgr.discard_deferred("s1") == 0


def test_discard_deferred_for_unknown_session_returns_zero() -> None:
    mgr = SurfaceManager()
    assert mgr.discard_deferred("ghost") == 0


def test_discard_deferred_does_not_clear_busy_flag() -> None:
    """Cancel discards the queue but the turn is still in flight
    (cancel is fired BEFORE the handler returns; turn-end fires
    later in the finally block).  The flag must stay True until
    mark_turn_end runs."""
    mgr, _ = _make_mgr_with_session()
    mgr.mark_turn_start("s1")

    async def _send_fn():
        return None

    asyncio.run(mgr.defer_or_send("s1", _send_fn))
    mgr.discard_deferred("s1")
    assert mgr.is_turn_busy("s1") is True
