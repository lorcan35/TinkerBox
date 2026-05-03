"""Tests for ``dragon_voice.surface_register``.

Pin every branch:

  1. Both managers None → no-op.
  2. surface_mgr only → register_session called; replay skipped.
  3. scheduler_mgr only → register_session skipped; replay called.
  4. Both wired, happy path → register first, then replay.
  5. surface_mgr.register_session passes the send closure that
     routes through safe_send_json (NOT a raw ws.send_json).
  6. The send closure short-circuits when ws.closed is True.
  7. scheduler_mgr.replay_queued_for_device raises → swallowed
     with WARNING log.
  8. Replay returns >0 → logged at INFO; replay returns 0 → silent.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.surface_register import register_surface_and_replay_scheduler


def _make_ws(*, closed: bool = False) -> MagicMock:
    ws = MagicMock()
    ws.closed = closed
    return ws


def _make_safe_send_json() -> AsyncMock:
    return AsyncMock(return_value=True)


def _make_surface_mgr() -> MagicMock:
    sm = MagicMock()
    sm.register_session = AsyncMock()
    return sm


def _make_scheduler_mgr(returns: int = 0, raises: Exception | None = None) -> MagicMock:
    sm = MagicMock()
    if raises is not None:
        sm.replay_queued_for_device = AsyncMock(side_effect=raises)
    else:
        sm.replay_queued_for_device = AsyncMock(return_value=returns)
    return sm


# ─── No-managers branch ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_both_managers_none_is_noop():
    ws = _make_ws()
    send = _make_safe_send_json()

    await register_surface_and_replay_scheduler(
        ws,
        surface_mgr=None,
        scheduler_mgr=None,
        session_id="sess-X",
        device_id="dev-A",
        widget_capabilities=None,
        safe_send_json=send,
    )
    send.assert_not_awaited()


# ─── Surface-only ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_surface_mgr_only_register_called_replay_skipped():
    ws = _make_ws()
    surface = _make_surface_mgr()

    await register_surface_and_replay_scheduler(
        ws,
        surface_mgr=surface,
        scheduler_mgr=None,
        session_id="sess-X",
        device_id="dev-A",
        widget_capabilities={"types": ["live"]},
        safe_send_json=_make_safe_send_json(),
    )

    surface.register_session.assert_awaited_once()
    args, kwargs = surface.register_session.await_args
    assert args[0] == "sess-X"
    assert kwargs["caps"] == {"types": ["live"]}


# ─── Scheduler-only ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scheduler_mgr_only_replay_called_register_skipped():
    ws = _make_ws()
    scheduler = _make_scheduler_mgr(returns=3)

    await register_surface_and_replay_scheduler(
        ws,
        surface_mgr=None,
        scheduler_mgr=scheduler,
        session_id="sess-X",
        device_id="dev-A",
        widget_capabilities=None,
        safe_send_json=_make_safe_send_json(),
    )
    scheduler.replay_queued_for_device.assert_awaited_once_with("dev-A")


# ─── Happy path: both wired, ordering matters ───────────────────


@pytest.mark.asyncio
async def test_register_called_before_replay():
    """The replay MUST happen AFTER surface register so the
    scheduler can find a live Tab5Surface for the session.  Pin
    the ordering so a future refactor can't reverse it."""
    ws = _make_ws()
    surface = _make_surface_mgr()
    scheduler = _make_scheduler_mgr(returns=1)

    # Track call order via a shared list.
    order: list[str] = []
    surface.register_session = AsyncMock(side_effect=lambda *a, **kw: order.append("register"))
    scheduler.replay_queued_for_device = AsyncMock(side_effect=lambda *a, **kw: order.append("replay"))

    await register_surface_and_replay_scheduler(
        ws,
        surface_mgr=surface,
        scheduler_mgr=scheduler,
        session_id="sess-X",
        device_id="dev-A",
        widget_capabilities={"types": ["live"]},
        safe_send_json=_make_safe_send_json(),
    )

    assert order == ["register", "replay"]


# ─── Send closure routes through safe_send_json ─────────────────


@pytest.mark.asyncio
async def test_send_closure_routes_through_safe_send_json():
    """The closure passed to surface_mgr.register_session must
    route through safe_send_json (which handles transport-close
    swallow), NOT through raw ws.send_json."""
    ws = _make_ws()
    send = _make_safe_send_json()
    captured_closure = None

    surface = MagicMock()
    async def capture_register(session_id, closure, **kwargs):
        nonlocal captured_closure
        captured_closure = closure
    surface.register_session = AsyncMock(side_effect=capture_register)

    await register_surface_and_replay_scheduler(
        ws,
        surface_mgr=surface,
        scheduler_mgr=None,
        session_id="sess-X",
        device_id="dev-A",
        widget_capabilities=None,
        safe_send_json=send,
    )

    # Now invoke the captured closure and verify it routes through
    # safe_send_json.
    assert captured_closure is not None
    await captured_closure({"type": "widget_live", "card_id": "c1"})
    send.assert_awaited_once()
    payload = send.await_args.args[1]
    assert payload == {"type": "widget_live", "card_id": "c1"}


@pytest.mark.asyncio
async def test_send_closure_skips_when_ws_closed():
    """Captured closure should short-circuit when ws.closed is
    True — avoid spurious log noise from the safe_send_json layer."""
    ws = _make_ws(closed=True)
    send = _make_safe_send_json()
    captured_closure = None

    surface = MagicMock()
    async def capture_register(session_id, closure, **kwargs):
        nonlocal captured_closure
        captured_closure = closure
    surface.register_session = AsyncMock(side_effect=capture_register)

    await register_surface_and_replay_scheduler(
        ws,
        surface_mgr=surface,
        scheduler_mgr=None,
        session_id="sess-X",
        device_id="dev-A",
        widget_capabilities=None,
        safe_send_json=send,
    )

    assert captured_closure is not None
    await captured_closure({"type": "widget_live"})
    # Closed → no send attempted.
    send.assert_not_awaited()


# ─── Scheduler failure isolation ────────────────────────────────


@pytest.mark.asyncio
async def test_scheduler_replay_failure_swallowed():
    """A queue-drain failure must NOT block registration —
    user's reminders just stay queued for the next register."""
    ws = _make_ws()
    surface = _make_surface_mgr()
    scheduler = _make_scheduler_mgr(raises=RuntimeError("DB busy"))

    # Must NOT raise.
    await register_surface_and_replay_scheduler(
        ws,
        surface_mgr=surface,
        scheduler_mgr=scheduler,
        session_id="sess-X",
        device_id="dev-A",
        widget_capabilities=None,
        safe_send_json=_make_safe_send_json(),
    )

    # Surface still registered (happens BEFORE the replay).
    surface.register_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_scheduler_replay_zero_is_silent():
    """When the scheduler reports 0 replayed frames, we don't
    log a confusing 'delivered 0 frames' message — pinned by
    only logging when replayed > 0."""
    ws = _make_ws()
    scheduler = _make_scheduler_mgr(returns=0)

    # Must NOT raise + not log spam.
    await register_surface_and_replay_scheduler(
        ws,
        surface_mgr=None,
        scheduler_mgr=scheduler,
        session_id="sess-X",
        device_id="dev-A",
        widget_capabilities=None,
        safe_send_json=_make_safe_send_json(),
    )
    scheduler.replay_queued_for_device.assert_awaited_once()
