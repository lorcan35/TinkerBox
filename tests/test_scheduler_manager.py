"""Async tests for SchedulerManager.

Phase 5 ε1a (refs #126, #128).

Pattern: stub the ``NotificationStore`` (use the real
InMemoryNotificationStore — it's already pure Python with no I/O,
no point mocking it), patch ``asyncio.sleep`` so the tests don't
burn wall-clock waits, and stub the SurfaceManager + SessionManager
collaborators.

Coverage map (RFC Section A9):
  * scheduled job fires at right time
  * cancel before fire suppresses fire (no widget_card sent)
  * manager shutdown cancels in-flight jobs
  * two jobs at same instant both fire
  * runaway cap enforced (RFC E2 — per-device cap of 100)
"""
from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.scheduler.manager import SchedulerManager
from dragon_voice.scheduler.models import Notification
from dragon_voice.scheduler.store import InMemoryNotificationStore


# ───────────────────────── helpers


def _make_manager(
    *,
    surface_for_returns: Any = None,
    active_session: dict | None = None,
) -> tuple[SchedulerManager, list[dict], InMemoryNotificationStore]:
    """Build a manager with a real InMemoryStore + stubbed surface_mgr +
    stubbed session_mgr.  Returns (manager, captured_card_emits, store).

    captured_card_emits is the list of widget_card payloads the
    stubbed Tab5Surface received via .card(...) — lets tests assert
    "the right frame was sent at the right time"."""
    store = InMemoryNotificationStore()

    captured_emits: list[dict] = []

    # Stub Tab5Surface — only .card() is exercised.  Returns a card_id
    # the manager doesn't actually care about (it built its own).
    fake_surface = MagicMock()

    async def _capture_card(**kwargs):
        captured_emits.append(kwargs)
        return kwargs.get("card_id", "stub_card_id")

    fake_surface.card = _capture_card

    # Stub SurfaceManager — surface_for(session_id, skill_id) returns
    # the fake surface (or None if the test wants "no active surface").
    fake_surface_mgr = MagicMock()
    if surface_for_returns is None:
        fake_surface_mgr.surface_for = MagicMock(return_value=fake_surface)
    else:
        fake_surface_mgr.surface_for = MagicMock(return_value=surface_for_returns)

    # Stub SessionManager — list_sessions(device_id, status="active")
    # returns either [active_session] or [] (offline / no session).
    fake_session_mgr = MagicMock()
    fake_session_mgr.list_sessions = AsyncMock(
        return_value=[active_session] if active_session else []
    )

    mgr = SchedulerManager(
        store=store,
        surface_mgr=fake_surface_mgr,
        session_mgr=fake_session_mgr,
    )
    return mgr, captured_emits, store


def _patch_sleep_pass_through(monkeypatch):
    """Replace ``asyncio.sleep`` inside scheduler.manager with a
    no-op that yields once.  Lets the manager's "sleep until fire_at"
    block return immediately so the test driver can observe the
    fire-side behaviour without burning real seconds."""
    real_sleep = asyncio.sleep

    async def fast_sleep(secs: float, *args, **kwargs):
        # Yield control once so other tasks can run, but don't wait.
        await real_sleep(0)

    import dragon_voice.scheduler.manager as mgr_mod
    monkeypatch.setattr(mgr_mod.asyncio, "sleep", fast_sleep)


# ───────────────────────── core scheduling


@pytest.mark.asyncio
async def test_schedule_fires_widget_card_at_fire_time(monkeypatch) -> None:
    """The headline contract: scheduling a notification creates an
    asyncio task that, when its sleep-to-fire-at returns, sends a
    widget_card to the device's active session via SurfaceManager."""
    _patch_sleep_pass_through(monkeypatch)
    active_session = {"id": "sess123", "device_id": "dev_A"}
    mgr, emits, store = _make_manager(active_session=active_session)

    notif = Notification(
        device_id="dev_A",
        fire_at=time.time() + 0.001,  # essentially now (sleep is patched)
        title="Reminder",
        body="Take out the trash",
    )
    await mgr.schedule(notif)

    # Drive the task to completion — the patched sleep returns
    # immediately, so the manager's _fire_one runs after one yield.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # Wait for the in-flight task to finish
    if notif.id in mgr._tasks:
        await mgr._tasks[notif.id]

    assert len(emits) == 1, f"expected 1 widget_card emit, got {len(emits)}: {emits}"
    payload = emits[0]
    assert payload["title"] == "Reminder"
    assert payload["body"] == "Take out the trash"
    assert payload["tone"] == "info"
    assert payload["skill_id"] == "scheduler"

    # Store reflects the fire
    after = await store.get(notif.id)
    assert after is not None
    assert after.status == "fired"


@pytest.mark.asyncio
async def test_cancel_before_fire_suppresses_widget_card(monkeypatch) -> None:
    """If we cancel a pending notification before its fire_at, the
    fire callback must NOT run."""
    _patch_sleep_pass_through(monkeypatch)
    active_session = {"id": "sess123", "device_id": "dev_A"}
    mgr, emits, store = _make_manager(active_session=active_session)

    notif = Notification(
        device_id="dev_A",
        fire_at=time.time() + 100,  # far future relative to wall clock
        title="X",
        body="Y",
    )
    await mgr.schedule(notif)

    # Cancel before sleep returns
    cancelled = await mgr.cancel(notif.id)
    assert cancelled is True

    # Drain any pending tasks
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert emits == [], f"expected no emits after cancel; got {emits}"
    after = await store.get(notif.id)
    assert after.status == "cancelled"


@pytest.mark.asyncio
async def test_manager_shutdown_cancels_inflight_tasks(monkeypatch) -> None:
    """Manager.shutdown() must cancel every pending asyncio task so
    Dragon graceful-shutdown doesn't see "Task was destroyed but it
    is pending!" warnings.  Pin the cancel-then-await pattern that
    fixed W14-M09."""
    _patch_sleep_pass_through(monkeypatch)
    active_session = {"id": "s", "device_id": "dev_A"}
    mgr, emits, store = _make_manager(active_session=active_session)

    n1 = Notification(device_id="dev_A", fire_at=time.time() + 100, title="A", body="")
    n2 = Notification(device_id="dev_A", fire_at=time.time() + 200, title="B", body="")
    await mgr.schedule(n1)
    await mgr.schedule(n2)

    assert len(mgr._tasks) == 2

    await mgr.shutdown()

    # All tracked tasks done (cancelled or finished)
    for task in mgr._tasks.values():
        assert task.done()

    # The store reflects: status stays pending (shutdown != cancel),
    # but no fire happened.
    n1_after = await store.get(n1.id)
    n2_after = await store.get(n2.id)
    assert n1_after.status == "pending"
    assert n2_after.status == "pending"


@pytest.mark.asyncio
async def test_runaway_cap_rejects_101st_notification(monkeypatch) -> None:
    """RFC E2 / R2: per-device cap of 100 pending notifications.
    The 101st must be rejected with a clear error so a hostile LLM
    or buggy caller can't DoS the manager.  The cap is enforced
    BEFORE store.create so the count stays accurate."""
    from dragon_voice.scheduler.manager import RunawayCapError

    _patch_sleep_pass_through(monkeypatch)
    active_session = {"id": "s", "device_id": "dev_A"}
    mgr, emits, store = _make_manager(active_session=active_session)

    # Schedule 100 with very-far-future fire_at so they all stay pending
    for i in range(100):
        await mgr.schedule(Notification(
            device_id="dev_A",
            fire_at=time.time() + 86400,
            title=f"R{i}",
            body="",
        ))

    # 101st must reject
    with pytest.raises(RunawayCapError):
        await mgr.schedule(Notification(
            device_id="dev_A",
            fire_at=time.time() + 86400,
            title="overflow",
            body="",
        ))

    # Store still has exactly 100 pending for this device
    pending = await store.list_pending(device_id="dev_A")
    assert len(pending) == 100


@pytest.mark.asyncio
async def test_schedule_when_no_active_session_marks_fired_anyway(monkeypatch) -> None:
    """Tier 1 contract (RFC A3 + R4): if the device has no active
    session at fire time, log + drop.  The notification still
    flips to ``fired`` in the store so it doesn't leak as pending
    forever.  Tier 2 will queue for replay; Tier 1 just drops."""
    _patch_sleep_pass_through(monkeypatch)
    # active_session=None → SessionManager.list_sessions returns []
    mgr, emits, store = _make_manager(active_session=None)

    notif = Notification(
        device_id="dev_offline",
        fire_at=time.time() + 0.001,
        title="x",
        body="y",
    )
    await mgr.schedule(notif)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    if notif.id in mgr._tasks:
        await mgr._tasks[notif.id]

    # No widget_card was emitted (no session to send to)
    assert emits == []
    # But the notification flipped to fired (Tier 1: dropped, not retried)
    after = await store.get(notif.id)
    assert after.status == "fired"


@pytest.mark.asyncio
async def test_reschedule_updates_fire_at(monkeypatch) -> None:
    """PATCH /api/v1/scheduler/notifications/{id} with new `when`
    must replace the in-flight task with one targeted at the new
    fire_at.  Pin so the asyncio task tracking doesn't leak the
    old task."""
    _patch_sleep_pass_through(monkeypatch)
    active_session = {"id": "s", "device_id": "dev_A"}
    mgr, emits, store = _make_manager(active_session=active_session)

    notif = Notification(
        device_id="dev_A",
        fire_at=time.time() + 100,
        title="x",
        body="y",
    )
    await mgr.schedule(notif)
    original_task = mgr._tasks[notif.id]

    new_fire = time.time() + 200
    rescheduled = await mgr.reschedule(notif.id, new_fire)
    assert rescheduled is True

    # The original task was cancelled, a new one tracked
    assert mgr._tasks[notif.id] is not original_task
    assert original_task.done()  # cancelled

    # Store reflects the new fire_at
    after = await store.get(notif.id)
    assert after.fire_at == pytest.approx(new_fire)
