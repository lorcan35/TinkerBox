"""Notification storage backends.

Phase 5 ε1a (refs #126, #128).

The ``NotificationStore`` Protocol is the single seam between
SchedulerManager and the durability layer.  ε1a ships
``InMemoryNotificationStore`` (everything-lost-on-restart, fine for
the audit's Tier-1 contract).  ε2 will add ``SqliteNotificationStore``
as a sibling class — same Protocol, no manager-side changes.

This is the architectural decision the RFC Section A8 calls "the
single most important architectural decision in the RFC because it
lets ε1 ship and bake without ε2 work being a rewrite".  Keep the
Protocol narrow (7 methods) and don't leak SQL-isms across it.
"""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from dragon_voice.scheduler.models import Notification


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

    async def count_pending_for_device(self, device_id: str) -> int:
        """Used by SchedulerManager to enforce the per-device runaway
        cap of 100 (RFC E2 / R2).  O(1) for in-memory; O(log N) via
        index for SQLite."""
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

    async def count_pending_for_device(self, device_id: str) -> int:
        return sum(
            1 for n in self._by_id.values()
            if n.status == "pending" and n.device_id == device_id
        )


__all__ = ["NotificationStore", "InMemoryNotificationStore"]
