"""Real-DB tests for SqliteNotificationStore.

Phase 5 ε2 (refs #126, #131).

Same pattern as test_paused_session_retention.py — real aiosqlite
+ ``tmp_path`` so the SQL is exercised end-to-end (would-be SQL
typo = test failure here, not a runtime surprise on the live
Dragon).  The InMemoryNotificationStore tests remain in
test_scheduler_manager.py as the manager-side coverage; this file
is the SQL-layer round-trip proof.

Coverage map (RFC Section A9):
  * round-trip create → get
  * list_pending filters by device_id
  * mark_fired flips status + stamps fired_at
  * list_due returns pending with fire_at <= now (hot path for boot replay)
  * cancel + update_fire_at + count_pending_for_device round-trips
  * queue_notification + drain_queue_for_device + cap-at-50 enforcement
"""
from __future__ import annotations

import json
import pathlib
import time

import pytest

from dragon_voice.db import Database
from dragon_voice.scheduler.models import Notification
from dragon_voice.scheduler.store import (
    NOTIFICATION_QUEUE_CAP_PER_DEVICE,
    SqliteNotificationStore,
)


# ───────────────────────── helpers


async def _make_db(tmp_path: pathlib.Path) -> Database:
    db = Database(str(tmp_path / "scheduler.db"))
    await db.initialize()
    # Insert a device so FK constraints don't reject the notification rows.
    now = time.time()
    await db.conn.execute(
        """INSERT OR IGNORE INTO devices (id, hardware_id, created_at, updated_at)
           VALUES ('dev_X', 'aa:bb:cc:dd:ee:ff', ?, ?)""",
        (now, now),
    )
    await db.conn.execute(
        """INSERT OR IGNORE INTO devices (id, hardware_id, created_at, updated_at)
           VALUES ('dev_Y', 'aa:bb:cc:dd:ee:00', ?, ?)""",
        (now, now),
    )
    await db.conn.commit()
    return db


def _notif(
    device_id: str = "dev_X",
    *,
    fire_in_seconds: float = 60,
    title: str = "T",
    body: str = "B",
    status: str = "pending",
) -> Notification:
    return Notification(
        device_id=device_id,
        fire_at=time.time() + fire_in_seconds,
        title=title,
        body=body,
        status=status,
    )


# ───────────────────────── round-trip


@pytest.mark.asyncio
async def test_create_then_get_round_trips(tmp_path: pathlib.Path) -> None:
    """Headline contract: a notification round-trips through SQLite
    with all fields intact.  Pin so a column-rename refactor would
    fail fast here, not silently lose data."""
    db = await _make_db(tmp_path)
    store = SqliteNotificationStore(db)
    try:
        n = _notif(title="round-trip", body="check all fields")
        await store.create(n)
        fetched = await store.get(n.id)
        assert fetched is not None
        assert fetched.id == n.id
        assert fetched.device_id == "dev_X"
        assert fetched.title == "round-trip"
        assert fetched.body == "check all fields"
        assert fetched.tone == "info"
        assert fetched.status == "pending"
        assert fetched.fire_at == pytest.approx(n.fire_at)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_list_pending_filters_by_device(tmp_path: pathlib.Path) -> None:
    """list_pending(device_id="dev_X") returns ONLY dev_X's pending —
    not dev_Y's, not cancelled, not fired."""
    db = await _make_db(tmp_path)
    store = SqliteNotificationStore(db)
    try:
        await store.create(_notif("dev_X", title="x_pending"))
        await store.create(_notif("dev_Y", title="y_pending"))
        cancelled = _notif("dev_X", title="x_cancel", status="cancelled")
        cancelled.cancelled_at = time.time()
        await store.create(cancelled)

        x_pending = await store.list_pending(device_id="dev_X")
        assert {n.title for n in x_pending} == {"x_pending"}

        # Without filter, both pending notifications come back.
        all_pending = await store.list_pending()
        assert {n.title for n in all_pending} == {"x_pending", "y_pending"}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_mark_fired_flips_status_and_stamps(tmp_path: pathlib.Path) -> None:
    """mark_fired sets status='fired' and stamps fired_at."""
    db = await _make_db(tmp_path)
    store = SqliteNotificationStore(db)
    try:
        n = _notif()
        await store.create(n)
        assert (await store.get(n.id)).status == "pending"

        await store.mark_fired(n.id)
        after = await store.get(n.id)
        assert after.status == "fired"
        assert after.fired_at is not None
        assert after.fired_at == pytest.approx(time.time(), abs=2.0)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_list_due_returns_only_pending_with_past_fire_at(
    tmp_path: pathlib.Path,
) -> None:
    """The boot-replay hot path: list_due(now) returns pending
    notifications whose fire_at is <= now.  Sorted by fire_at so
    replay processes the oldest first (matches FIFO user
    expectation)."""
    db = await _make_db(tmp_path)
    store = SqliteNotificationStore(db)
    try:
        # Past (due)
        n_past1 = Notification(device_id="dev_X", fire_at=time.time() - 60, title="past1", body="")
        n_past2 = Notification(device_id="dev_X", fire_at=time.time() - 120, title="past2", body="")
        # Future (not due)
        n_future = Notification(device_id="dev_X", fire_at=time.time() + 60, title="future", body="")
        # Past but already fired (must NOT appear)
        n_fired = Notification(device_id="dev_X", fire_at=time.time() - 30,
                               title="fired", body="", status="fired")
        n_fired.fired_at = time.time() - 25
        for n in (n_past1, n_past2, n_future, n_fired):
            await store.create(n)

        due = await store.list_due(time.time())
        ids = {n.id for n in due}
        assert ids == {n_past1.id, n_past2.id}
        # Sort order: oldest first
        assert due[0].title == "past2"
        assert due[1].title == "past1"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cancel_updates_status_and_stamps(tmp_path: pathlib.Path) -> None:
    db = await _make_db(tmp_path)
    store = SqliteNotificationStore(db)
    try:
        n = _notif()
        await store.create(n)
        ok = await store.cancel(n.id)
        assert ok is True
        after = await store.get(n.id)
        assert after.status == "cancelled"
        assert after.cancelled_at == pytest.approx(time.time(), abs=2.0)

        # Idempotent: second cancel returns False (already non-pending)
        ok2 = await store.cancel(n.id)
        assert ok2 is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_update_fire_at_for_pending(tmp_path: pathlib.Path) -> None:
    db = await _make_db(tmp_path)
    store = SqliteNotificationStore(db)
    try:
        n = _notif(fire_in_seconds=60)
        await store.create(n)
        new_fire = time.time() + 200
        ok = await store.update_fire_at(n.id, new_fire)
        assert ok is True
        after = await store.get(n.id)
        assert after.fire_at == pytest.approx(new_fire)

        # Updating a fired notification must reject
        await store.mark_fired(n.id)
        bad = await store.update_fire_at(n.id, time.time() + 300)
        assert bad is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_count_pending_for_device(tmp_path: pathlib.Path) -> None:
    """Used by the runaway-cap check — must match list_pending count
    so the cap check doesn't drift from the actual state."""
    db = await _make_db(tmp_path)
    store = SqliteNotificationStore(db)
    try:
        for _ in range(7):
            await store.create(_notif("dev_X"))
        for _ in range(3):
            await store.create(_notif("dev_Y"))
        assert await store.count_pending_for_device("dev_X") == 7
        assert await store.count_pending_for_device("dev_Y") == 3
        assert await store.count_pending_for_device("dev_Z") == 0
    finally:
        await db.close()


# ───────────────────────── notification_queue


@pytest.mark.asyncio
async def test_queue_notification_and_drain(tmp_path: pathlib.Path) -> None:
    """Queue a fired-but-undelivered notification (offline device),
    then drain on reconnect.  Drain returns the rendered payloads
    in FIFO order and clears the rows."""
    db = await _make_db(tmp_path)
    store = SqliteNotificationStore(db)
    try:
        # Queue 3 frames for dev_X
        await store.queue_notification(
            "dev_X", "sched_001", {"type": "widget_card", "body": "1st"}
        )
        await store.queue_notification(
            "dev_X", "sched_002", {"type": "widget_card", "body": "2nd"}
        )
        # And one for dev_Y so we can verify per-device drain
        await store.queue_notification(
            "dev_Y", "sched_003", {"type": "widget_card", "body": "y1"}
        )

        drained = await store.drain_queue_for_device("dev_X")
        assert len(drained) == 2
        # FIFO order
        assert drained[0]["body"] == "1st"
        assert drained[1]["body"] == "2nd"

        # dev_X queue empty after drain
        re_drain = await store.drain_queue_for_device("dev_X")
        assert re_drain == []

        # dev_Y still has its frame
        y_drain = await store.drain_queue_for_device("dev_Y")
        assert len(y_drain) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_queue_drops_oldest_at_cap(tmp_path: pathlib.Path) -> None:
    """RFC R5: per-device queue cap of 50 enforced by drop-oldest at
    insert.  Pin so a future cap bump (or lift) is a deliberate
    constant change here, not silent unbounded growth."""
    db = await _make_db(tmp_path)
    store = SqliteNotificationStore(db)
    try:
        # Insert NOTIFICATION_QUEUE_CAP_PER_DEVICE + 5 items
        for i in range(NOTIFICATION_QUEUE_CAP_PER_DEVICE + 5):
            await store.queue_notification(
                "dev_X", f"sched_{i:03d}",
                {"type": "widget_card", "body": f"item_{i}"},
            )

        drained = await store.drain_queue_for_device("dev_X")
        # Cap enforced — at most NOTIFICATION_QUEUE_CAP_PER_DEVICE rows
        assert len(drained) == NOTIFICATION_QUEUE_CAP_PER_DEVICE
        # Oldest 5 dropped; newest survived (item_5 ... item_54)
        bodies = {d["body"] for d in drained}
        assert "item_0" not in bodies
        assert "item_4" not in bodies
        assert f"item_{NOTIFICATION_QUEUE_CAP_PER_DEVICE + 4}" in bodies
    finally:
        await db.close()
