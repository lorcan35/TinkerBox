"""Tests for ε2 boot replay + offline queue + snooze paths.

Phase 5 ε2 (refs #126, #131).

These exercise the manager's NEW behaviour added in ε2:
  * SchedulerManager.start() reads list_due(now) and reschedules
    each, with a 15-minute "expired" window beyond which due-but-
    unfired notifications mark `failed` (RFC R8)
  * _fire_one calls store.queue_notification when no active session
    exists (instead of just dropping per Tier 1)
  * SchedulerManager.replay_queued_for_device(device_id) is the
    server.py register-time hook — drains the queue + sends frames
    via SurfaceManager + paces at 100ms (RFC R5)
  * snooze action handler reschedules the notification
"""
from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.scheduler.manager import (
    REPLAY_PACING_SECONDS,
    REPLAY_WINDOW_SECONDS,
    SchedulerManager,
)
from dragon_voice.scheduler.models import Notification
from dragon_voice.scheduler.store import InMemoryNotificationStore


# Use the in-memory store with manually-seeded data — exercises the
# manager's replay logic without a real DB round-trip (the SQL is
# already covered by test_scheduler_store_sqlite.py).


def _make_manager_with_active_session() -> tuple[
    SchedulerManager, list[dict], InMemoryNotificationStore,
]:
    """Same helper as test_scheduler_manager.py but extracted for
    re-use here.  Captures emitted widget_card payloads."""
    store = InMemoryNotificationStore()
    captured: list[dict] = []

    fake_surface = MagicMock()

    async def _capture_card(**kwargs):
        captured.append(kwargs)
        return kwargs.get("card_id", "stub")

    fake_surface.card = _capture_card
    surface_mgr = MagicMock()
    surface_mgr.surface_for = MagicMock(return_value=fake_surface)
    # Audit B1 (#165): defer_or_send routes the live-fire path
    # through the turn-gate; in tests we run it immediately.
    async def _defer_or_send(session_id, send_fn):
        await send_fn()
        return True
    surface_mgr.defer_or_send = _defer_or_send

    session_mgr = MagicMock()
    session_mgr.list_sessions = AsyncMock(
        return_value=[{"id": "sess1", "device_id": "dev_X"}]
    )

    mgr = SchedulerManager(
        store=store, surface_mgr=surface_mgr, session_mgr=session_mgr,
    )
    return mgr, captured, store


# ───────────────────────── boot replay


@pytest.mark.asyncio
async def test_start_replays_due_notifications_within_window(monkeypatch) -> None:
    """The headline ε2 contract: SchedulerManager.start() picks up
    notifications whose fire_at is past but status is still
    'pending' (Dragon was restarted before they fired) and
    reschedules them as in-flight asyncio tasks."""
    # Patch sleep to a no-op for the in-flight task
    real_sleep = asyncio.sleep

    async def fast_sleep(secs: float, *args, **kwargs):
        await real_sleep(0)

    import dragon_voice.scheduler.manager as mgr_mod
    monkeypatch.setattr(mgr_mod.asyncio, "sleep", fast_sleep)

    mgr, captured, store = _make_manager_with_active_session()

    # Seed a "stranded" pending notification with fire_at in the
    # past but within the 15-minute replay window.
    stranded = Notification(
        device_id="dev_X",
        fire_at=time.time() - 60,  # 1 minute ago — within window
        title="boot_replay",
        body="should fire on start",
    )
    await store.create(stranded)

    await mgr.start()

    # Drive the rescheduled task to completion
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    if stranded.id in mgr._tasks:
        await mgr._tasks[stranded.id]

    # The notification was fired — widget_card landed
    assert len(captured) == 1, f"expected 1 emit, got {captured}"
    assert captured[0]["body"] == "should fire on start"
    after = await store.get(stranded.id)
    assert after.status == "fired"


@pytest.mark.asyncio
async def test_start_recreates_tasks_for_future_pending(monkeypatch) -> None:
    """The common case after a routine Dragon restart: a notification
    is still pending, fire_at is FUTURE.  The asyncio task that was
    sleeping toward it died with the prior process — boot replay
    must recreate the task so the notification still fires at the
    original time.

    Caught by live testing — initial implementation only handled
    past-due notifications via list_due(), leaving future-pending
    rows orphaned in SQLite without any in-flight task.
    """
    real_sleep = asyncio.sleep

    async def fast_sleep(secs: float, *args, **kwargs):
        await real_sleep(0)

    import dragon_voice.scheduler.manager as mgr_mod
    monkeypatch.setattr(mgr_mod.asyncio, "sleep", fast_sleep)

    mgr, captured, store = _make_manager_with_active_session()

    future = Notification(
        device_id="dev_X",
        fire_at=time.time() + 60,  # future, not yet due
        title="future_pending",
        body="should still fire after restart",
    )
    await store.create(future)

    await mgr.start()

    # Drive the rescheduled task to completion (sleep is mocked)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    if future.id in mgr._tasks:
        await mgr._tasks[future.id]

    # The future-pending notification fired
    assert len(captured) == 1
    assert captured[0]["body"] == "should still fire after restart"
    after = await store.get(future.id)
    assert after.status == "fired"


@pytest.mark.asyncio
async def test_start_marks_expired_due_as_failed_outside_window() -> None:
    """RFC R8: notifications older than REPLAY_WINDOW_SECONDS (15
    min default) are NOT replayed — marked `failed` instead so they
    don't surface stale info to the user."""
    mgr, captured, store = _make_manager_with_active_session()

    stranded = Notification(
        device_id="dev_X",
        fire_at=time.time() - REPLAY_WINDOW_SECONDS - 60,  # 1 min beyond window
        title="too_old",
        body="should NOT fire",
    )
    await store.create(stranded)

    await mgr.start()

    # No widget_card emitted, status flipped to failed
    await asyncio.sleep(0)
    after = await store.get(stranded.id)
    assert after.status == "failed"
    assert captured == []


# ───────────────────────── offline queue write


@pytest.mark.asyncio
async def test_fire_when_offline_queues_payload(monkeypatch) -> None:
    """RFC R4 + R5: if device has no active session at fire time,
    the rendered widget_card payload is written to the
    notification_queue for later replay (instead of dropping).

    Exercises the in-memory store's no-op queue path AND verifies
    the manager called queue_notification with the right shape.
    """
    real_sleep = asyncio.sleep

    async def fast_sleep(secs: float, *args, **kwargs):
        await real_sleep(0)

    import dragon_voice.scheduler.manager as mgr_mod
    monkeypatch.setattr(mgr_mod.asyncio, "sleep", fast_sleep)

    # Build a manager with NO active session (offline device)
    store = InMemoryNotificationStore()
    surface_mgr = MagicMock()
    surface_mgr.surface_for = MagicMock(return_value=None)
    session_mgr = MagicMock()
    session_mgr.list_sessions = AsyncMock(return_value=[])  # no active session

    # Wrap the store to capture queue calls
    queued_calls: list[tuple] = []
    real_queue = store.queue_notification

    async def spy_queue(device_id, notification_id, payload):
        queued_calls.append((device_id, notification_id, payload))
        await real_queue(device_id, notification_id, payload)

    store.queue_notification = spy_queue

    mgr = SchedulerManager(
        store=store, surface_mgr=surface_mgr, session_mgr=session_mgr,
    )

    notif = Notification(
        device_id="dev_offline",
        fire_at=time.time() + 0.001,
        title="offline_test",
        body="queue this",
    )
    await mgr.schedule(notif)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    if notif.id in mgr._tasks:
        await mgr._tasks[notif.id]

    # Queue was written
    assert len(queued_calls) == 1
    device_id, notif_id, payload = queued_calls[0]
    assert device_id == "dev_offline"
    assert notif_id == notif.id
    # Payload is the rendered widget_card frame
    assert payload["type"] == "widget_card"
    assert payload["body"] == "queue this"
    assert payload["skill_id"] == "scheduler"
    # Notification still flipped to fired (not retried)
    after = await store.get(notif.id)
    assert after.status == "fired"


# ───────────────────────── replay on device register


@pytest.mark.asyncio
async def test_replay_queued_for_device_drains_and_paces(monkeypatch) -> None:
    """Server.py's register hook calls
    ``mgr.replay_queued_for_device(device_id)`` when a Tab5
    reconnects.  Each queued frame is sent + paced 100ms apart
    (RFC R5 — don't slam the WS receive buffer)."""
    real_sleep = asyncio.sleep
    sleep_calls: list[float] = []

    async def recording_sleep(secs: float, *args, **kwargs):
        sleep_calls.append(secs)
        await real_sleep(0)  # don't actually wait

    import dragon_voice.scheduler.manager as mgr_mod
    monkeypatch.setattr(mgr_mod.asyncio, "sleep", recording_sleep)

    mgr, captured, store = _make_manager_with_active_session()

    # Pre-seed the queue with 3 frames as if 3 reminders fired offline
    payload_a = {"type": "widget_card", "body": "1st", "skill_id": "scheduler"}
    payload_b = {"type": "widget_card", "body": "2nd", "skill_id": "scheduler"}
    payload_c = {"type": "widget_card", "body": "3rd", "skill_id": "scheduler"}
    # InMemoryStore's queue is a no-op — switch to a captured list
    drained = [payload_a, payload_b, payload_c]
    store.drain_queue_for_device = AsyncMock(return_value=drained)

    await mgr.replay_queued_for_device("dev_X")

    # All 3 frames were sent
    assert len(captured) == 3
    bodies = [c["body"] for c in captured]
    assert bodies == ["1st", "2nd", "3rd"]

    # Pacing: at least 2 sleep(REPLAY_PACING_SECONDS) calls between
    # the 3 frames (sleeps 1+2 — no sleep before frame 1, no sleep
    # after frame 3 either).
    pacing_calls = [s for s in sleep_calls if s == REPLAY_PACING_SECONDS]
    assert len(pacing_calls) >= 2


@pytest.mark.asyncio
async def test_replay_with_empty_queue_is_noop() -> None:
    """Calling replay_queued_for_device when the queue is empty must
    not error.  Pin so a Tab5 reconnect always hits the replay
    code path safely (the register hook can't conditional-skip
    based on queue state without a query of its own)."""
    mgr, captured, store = _make_manager_with_active_session()
    store.drain_queue_for_device = AsyncMock(return_value=[])
    await mgr.replay_queued_for_device("dev_X")
    assert captured == []


# ───────────────────────── snooze handler


@pytest.mark.asyncio
async def test_snooze_handler_reschedules_notification(monkeypatch) -> None:
    """RFC A6: snooze ships in Tier 2.  Tapping the snooze action
    on a fired widget_card reschedules the notification for N
    minutes later via the existing manager.reschedule path."""
    real_sleep = asyncio.sleep

    async def fast_sleep(secs: float, *args, **kwargs):
        await real_sleep(0)

    import dragon_voice.scheduler.manager as mgr_mod
    monkeypatch.setattr(mgr_mod.asyncio, "sleep", fast_sleep)

    mgr, captured, store = _make_manager_with_active_session()

    notif = Notification(
        device_id="dev_X",
        fire_at=time.time() + 60,
        title="snoozable",
        body="x",
    )
    await mgr.schedule(notif)
    original_fire_at = notif.fire_at

    # Simulate the action event Tab5 sends back when user taps
    # "Snooze 10m" (action.event="scheduler.snooze_10").  The handler
    # parses the event, reschedules the notification.
    handled = await mgr.handle_snooze(notif.id, snooze_minutes=10)
    assert handled is True

    after = await store.get(notif.id)
    # New fire_at is roughly 10 minutes later
    assert after.fire_at > original_fire_at
    assert after.fire_at == pytest.approx(time.time() + 10 * 60, abs=2.0)
    # Status stays pending
    assert after.status == "pending"
