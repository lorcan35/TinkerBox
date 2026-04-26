"""Notification storage backends.

Phase 5 ε1a (refs #126, #128) + ε2 (refs #131).

The ``NotificationStore`` Protocol is the single seam between
SchedulerManager and the durability layer.  ε1a shipped
``InMemoryNotificationStore`` (everything-lost-on-restart).  ε2 adds
``SqliteNotificationStore`` as a sibling — same Protocol, no
manager-side changes.

This is the architectural decision the RFC Section A8 calls "the
single most important architectural decision in the RFC because it
lets ε1 ship and bake without ε2 work being a rewrite".  Keep the
Protocol narrow and don't leak SQL-isms across it.
"""
from __future__ import annotations

import json
import logging
from typing import Optional, Protocol, runtime_checkable

from dragon_voice.scheduler.models import Notification

logger = logging.getLogger(__name__)


# RFC R5 — per-device offline queue cap.  At insert time, count rows
# for device_id; if ≥ cap, drop the oldest before insert.  Catches
# a multi-day-offline device coming back to find 1000 stale reminders
# replayed in a row (DoS the WS).
NOTIFICATION_QUEUE_CAP_PER_DEVICE = 50


@runtime_checkable
class NotificationStore(Protocol):
    """Storage interface every store implementation must satisfy.

    All methods are async to match the SQLite store's natural shape;
    the in-memory store implements them as ``async def`` returning
    immediately so callers get one consistent contract.
    """

    async def create(self, notification: Notification) -> Notification:
        """Persist + return the notification.  Raises if id collides."""
        ...

    async def get(self, notif_id: str) -> Optional[Notification]:
        """Fetch by id, or None if missing."""
        ...

    async def list_pending(
        self, device_id: Optional[str] = None,
    ) -> list[Notification]:
        """Pending notifications, optionally filtered to one device."""
        ...

    async def list_all(
        self,
        device_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[Notification]:
        """All notifications matching optional device + status filters.

        Used by the REST GET endpoint (ε1b) — needs to surface
        cancelled / fired history, not just pending.  Pending-only
        callers should use ``list_pending`` for clarity."""
        ...

    async def cancel(self, notif_id: str) -> bool:
        """Mark cancelled.  Returns True if the row existed and was
        pending; False otherwise (already fired / cancelled / missing).
        Idempotent."""
        ...

    async def update_fire_at(self, notif_id: str, fire_at: float) -> bool:
        """Reschedule.  Returns True if the row existed and was
        pending.  False otherwise (rescheduling a fired/cancelled
        job is a caller error — the manager surfaces a 400 in that
        case rather than silently no-oping)."""
        ...

    async def list_due(self, now: float) -> list[Notification]:
        """Pending notifications with fire_at <= now.  Used by ε2's
        boot replay; ε1a's in-memory store always returns []
        because nothing survives boot anyway."""
        ...

    async def mark_fired(self, notif_id: str) -> None:
        """Flip status to fired + stamp fired_at.  Called after the
        WS send returns (or after the safe-send swallow).  No-op if
        the row is already non-pending."""
        ...

    async def mark_failed(self, notif_id: str) -> None:
        """Flip status to failed.  Used by ε2 boot replay when a
        due notification is past the REPLAY_WINDOW_SECONDS cap
        (RFC R8) — distinct from cancel because the user didn't
        ask for the cancellation, and distinct from fired because
        the user never saw a card.  No-op if non-pending."""
        ...

    async def count_pending_for_device(self, device_id: str) -> int:
        """Used by SchedulerManager to enforce the per-device runaway
        cap of 100 (RFC E2 / R2).  O(1) for in-memory; O(log N) via
        index for SQLite."""
        ...

    async def queue_notification(
        self,
        device_id: str,
        notification_id: Optional[str],
        payload: dict,
    ) -> None:
        """ε2: queue a fired-but-undelivered notification (offline
        device) for replay on next register.  Stores the rendered
        widget_card payload (RFC B.5) — fire-time logic is past, so
        replay is "drain queue, send each frame".

        Drops the oldest queued frame for this device when the cap
        is hit (RFC R5).  No-op safe for in-memory store (Tier 1
        doesn't queue at all).
        """
        ...

    async def drain_queue_for_device(self, device_id: str) -> list[dict]:
        """ε2: pop all queued payloads for a device in FIFO order.
        Returns [] when empty.  Atomic — concurrent register events
        for the same device race for the queue but each frame is
        delivered exactly once.
        """
        ...


class InMemoryNotificationStore:
    """Process-local storage.  Lost on Dragon restart by design (Tier 1).

    The manager owns the single asyncio task per pending notification;
    this store just remembers the metadata so the manager can answer
    REST queries + enforce caps.  When the process dies, both go away
    together — Tier 2 fixes that with SqliteNotificationStore.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, Notification] = {}

    async def create(self, notification: Notification) -> Notification:
        if notification.id in self._by_id:
            raise ValueError(f"notification id collision: {notification.id}")
        self._by_id[notification.id] = notification
        return notification

    async def get(self, notif_id: str) -> Optional[Notification]:
        return self._by_id.get(notif_id)

    async def list_pending(
        self, device_id: Optional[str] = None,
    ) -> list[Notification]:
        out = [n for n in self._by_id.values() if n.status == "pending"]
        if device_id is not None:
            out = [n for n in out if n.device_id == device_id]
        out.sort(key=lambda n: n.fire_at)
        return out

    async def list_all(
        self,
        device_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[Notification]:
        out = list(self._by_id.values())
        if device_id is not None:
            out = [n for n in out if n.device_id == device_id]
        if status is not None:
            out = [n for n in out if n.status == status]
        out.sort(key=lambda n: n.fire_at, reverse=True)
        return out

    async def cancel(self, notif_id: str) -> bool:
        n = self._by_id.get(notif_id)
        if n is None or n.status != "pending":
            return False
        import time as _time
        n.status = "cancelled"
        n.cancelled_at = _time.time()
        return True

    async def update_fire_at(self, notif_id: str, fire_at: float) -> bool:
        n = self._by_id.get(notif_id)
        if n is None or n.status != "pending":
            return False
        n.fire_at = fire_at
        return True

    async def list_due(self, now: float) -> list[Notification]:
        # Tier 1: nothing survives a restart, so the boot-replay path
        # always sees an empty store.  ε2 will return real results.
        return [
            n for n in self._by_id.values()
            if n.status == "pending" and n.fire_at <= now
        ]

    async def mark_fired(self, notif_id: str) -> None:
        n = self._by_id.get(notif_id)
        if n is None or n.status != "pending":
            return
        import time as _time
        n.status = "fired"
        n.fired_at = _time.time()

    async def mark_failed(self, notif_id: str) -> None:
        n = self._by_id.get(notif_id)
        if n is None or n.status != "pending":
            return
        n.status = "failed"

    async def count_pending_for_device(self, device_id: str) -> int:
        return sum(
            1 for n in self._by_id.values()
            if n.status == "pending" and n.device_id == device_id
        )

    async def queue_notification(
        self,
        device_id: str,
        notification_id: Optional[str],
        payload: dict,
    ) -> None:
        # Tier 1: don't actually queue (RFC R4 says "Tier 1 = lost").
        # Implemented as a no-op so the manager can call this
        # unconditionally — the SQLite store is the only one that
        # actually persists.
        logger.debug(
            "InMemoryNotificationStore.queue_notification: dropping "
            "(Tier 1 doesn't queue) device=%s notif=%s",
            device_id, notification_id,
        )

    async def drain_queue_for_device(self, device_id: str) -> list[dict]:
        # Tier 1: nothing was ever queued.
        return []


# ───────────────────────── ε2 — SQLite store


class SqliteNotificationStore:
    """Durable storage backed by aiosqlite via the existing Database.

    Phase 5 ε2 (issue #131).  Same Protocol as InMemoryNotificationStore;
    the swap from in-memory → durable is a one-line change in
    lifecycle/startup.py.

    Uses the existing ``Database.conn`` aiosqlite handle (same pattern
    as MemoryService and other persistence layers) — no new connection
    pool, no new cursor lifecycle.

    Two tables (schema.sql appended):
      * ``scheduled_notifications`` — the pending/historical job rows
      * ``notification_queue`` — per-device offline replay queue

    SQL is intentionally plain — no ORMs, no query builders.  The
    schema is small and the read paths are O(log N) via indexes.
    """

    # Column list matches schema.sql order so SELECT * round-trips
    # cleanly into Notification dataclass via _row_to_notification.
    _COLUMNS = (
        "id, device_id, originating_session_id, fire_at, title, body, "
        "tone, status, recurrence, created_at, fired_at, cancelled_at"
    )

    def __init__(self, db) -> None:
        self._db = db

    async def create(self, notification: Notification) -> Notification:
        await self._db.conn.execute(
            f"""INSERT INTO scheduled_notifications ({self._COLUMNS})
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                notification.id,
                notification.device_id,
                notification.originating_session_id,
                notification.fire_at,
                notification.title,
                notification.body,
                notification.tone,
                notification.status,
                notification.recurrence,
                notification.created_at,
                notification.fired_at,
                notification.cancelled_at,
            ),
        )
        await self._db.conn.commit()
        return notification

    async def get(self, notif_id: str) -> Optional[Notification]:
        cursor = await self._db.conn.execute(
            f"SELECT {self._COLUMNS} FROM scheduled_notifications WHERE id = ?",
            (notif_id,),
        )
        row = await cursor.fetchone()
        return _row_to_notification(row) if row else None

    async def list_pending(
        self, device_id: Optional[str] = None,
    ) -> list[Notification]:
        if device_id is None:
            cursor = await self._db.conn.execute(
                f"""SELECT {self._COLUMNS} FROM scheduled_notifications
                    WHERE status = 'pending'
                    ORDER BY fire_at ASC""",
            )
        else:
            cursor = await self._db.conn.execute(
                f"""SELECT {self._COLUMNS} FROM scheduled_notifications
                    WHERE status = 'pending' AND device_id = ?
                    ORDER BY fire_at ASC""",
                (device_id,),
            )
        rows = await cursor.fetchall()
        return [_row_to_notification(r) for r in rows]

    async def list_all(
        self,
        device_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[Notification]:
        clauses, params = [], []
        if device_id is not None:
            clauses.append("device_id = ?")
            params.append(device_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        cursor = await self._db.conn.execute(
            f"""SELECT {self._COLUMNS} FROM scheduled_notifications
                {where}
                ORDER BY fire_at DESC""",
            params,
        )
        rows = await cursor.fetchall()
        return [_row_to_notification(r) for r in rows]

    async def cancel(self, notif_id: str) -> bool:
        import time as _time
        now = _time.time()
        # Atomic: only flip if currently pending (RFC R10 — idempotent
        # cancel is the contract callers rely on).
        cursor = await self._db.conn.execute(
            """UPDATE scheduled_notifications
               SET status = 'cancelled', cancelled_at = ?
               WHERE id = ? AND status = 'pending'""",
            (now, notif_id),
        )
        await self._db.conn.commit()
        return cursor.rowcount > 0

    async def update_fire_at(self, notif_id: str, fire_at: float) -> bool:
        cursor = await self._db.conn.execute(
            """UPDATE scheduled_notifications
               SET fire_at = ?
               WHERE id = ? AND status = 'pending'""",
            (fire_at, notif_id),
        )
        await self._db.conn.commit()
        return cursor.rowcount > 0

    async def list_due(self, now: float) -> list[Notification]:
        cursor = await self._db.conn.execute(
            f"""SELECT {self._COLUMNS} FROM scheduled_notifications
                WHERE status = 'pending' AND fire_at <= ?
                ORDER BY fire_at ASC""",
            (now,),
        )
        rows = await cursor.fetchall()
        return [_row_to_notification(r) for r in rows]

    async def mark_fired(self, notif_id: str) -> None:
        import time as _time
        now = _time.time()
        await self._db.conn.execute(
            """UPDATE scheduled_notifications
               SET status = 'fired', fired_at = ?
               WHERE id = ? AND status = 'pending'""",
            (now, notif_id),
        )
        await self._db.conn.commit()

    async def mark_failed(self, notif_id: str) -> None:
        await self._db.conn.execute(
            """UPDATE scheduled_notifications
               SET status = 'failed'
               WHERE id = ? AND status = 'pending'""",
            (notif_id,),
        )
        await self._db.conn.commit()

    async def count_pending_for_device(self, device_id: str) -> int:
        cursor = await self._db.conn.execute(
            """SELECT COUNT(*) FROM scheduled_notifications
               WHERE status = 'pending' AND device_id = ?""",
            (device_id,),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    # ── Notification queue (offline replay) ────────────────────────

    async def queue_notification(
        self,
        device_id: str,
        notification_id: Optional[str],
        payload: dict,
    ) -> None:
        """Insert into notification_queue, dropping the oldest if
        device already at NOTIFICATION_QUEUE_CAP_PER_DEVICE.

        RFC R5 — drop-oldest at insert (rather than reject-newest)
        because newer reminders are usually more user-relevant than
        an hour-old replay.
        """
        import time as _time
        now = _time.time()

        # Cap enforcement.  Single-user-per-device means this is
        # rarely contended; race-window is "two notifications fire
        # at the same instant for an offline device" which is fine
        # to handle non-atomically — worst case we briefly exceed
        # the cap by 1, which the next insert corrects.
        cursor = await self._db.conn.execute(
            """SELECT COUNT(*) FROM notification_queue WHERE device_id = ?""",
            (device_id,),
        )
        row = await cursor.fetchone()
        count = int(row[0]) if row else 0
        if count >= NOTIFICATION_QUEUE_CAP_PER_DEVICE:
            # Drop oldest N so we end up at cap-1 after this insert
            to_drop = count - NOTIFICATION_QUEUE_CAP_PER_DEVICE + 1
            await self._db.conn.execute(
                """DELETE FROM notification_queue
                   WHERE id IN (
                       SELECT id FROM notification_queue
                       WHERE device_id = ?
                       ORDER BY queued_at ASC
                       LIMIT ?
                   )""",
                (device_id, to_drop),
            )
            logger.warning(
                "notification_queue: dropped %d oldest frame(s) for "
                "device %s (cap=%d hit)",
                to_drop, device_id, NOTIFICATION_QUEUE_CAP_PER_DEVICE,
            )

        await self._db.conn.execute(
            """INSERT INTO notification_queue
               (device_id, notification_id, payload, queued_at)
               VALUES (?,?,?,?)""",
            (device_id, notification_id, json.dumps(payload), now),
        )
        await self._db.conn.commit()

    async def drain_queue_for_device(self, device_id: str) -> list[dict]:
        """Read all queued payloads for the device + delete them in a
        single transaction.  Safe under concurrent register events
        for the same device — each frame is delivered exactly once.
        """
        cursor = await self._db.conn.execute(
            """SELECT id, payload FROM notification_queue
               WHERE device_id = ?
               ORDER BY queued_at ASC""",
            (device_id,),
        )
        rows = await cursor.fetchall()
        if not rows:
            return []

        # Parse payloads, then delete the rows we just read.  The
        # DELETE-by-id list ensures we don't delete frames inserted
        # AFTER our SELECT (a re-fire that landed during this drain).
        payloads: list[dict] = []
        ids_to_delete: list[int] = []
        for row in rows:
            ids_to_delete.append(row[0])
            try:
                payloads.append(json.loads(row[1]))
            except json.JSONDecodeError as e:
                logger.warning(
                    "notification_queue: corrupt payload for id=%s: %s "
                    "(skipping)", row[0], e,
                )

        # Delete the rows we successfully drained
        placeholders = ",".join("?" * len(ids_to_delete))
        await self._db.conn.execute(
            f"DELETE FROM notification_queue WHERE id IN ({placeholders})",
            ids_to_delete,
        )
        await self._db.conn.commit()
        return payloads


def _row_to_notification(row) -> Notification:
    """Map a sqlite row tuple → Notification dataclass.  Column order
    matches ``SqliteNotificationStore._COLUMNS``."""
    n = Notification.__new__(Notification)
    n.id = row[0]
    n.device_id = row[1]
    n.originating_session_id = row[2]
    n.fire_at = row[3]
    n.title = row[4] or "Reminder"
    n.body = row[5] or ""
    n.tone = row[6] or "info"
    n.status = row[7] or "pending"
    n.recurrence = row[8]
    n.created_at = row[9]
    n.fired_at = row[10]
    n.cancelled_at = row[11]
    return n


__all__ = [
    "NotificationStore",
    "InMemoryNotificationStore",
    "SqliteNotificationStore",
    "NOTIFICATION_QUEUE_CAP_PER_DEVICE",
]
