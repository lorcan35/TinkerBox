"""SchedulerManager — owns the asyncio task per pending notification.

Phase 5 ε1a (refs #126, #128).  See docs/RFC-scheduler.md Section B.4
for the lifecycle wiring + Section A8 for why the
NotificationStore Protocol matters.

The manager:
  * holds a ``dict[notif_id -> asyncio.Task]`` so cancel + reschedule
    can find the right task to cancel
  * delegates persistence to a ``NotificationStore`` (in-memory in
    ε1a, SQLite in ε2 — same Protocol)
  * delegates UI delivery to ``Tab5Surface.card(...)`` via SurfaceManager
  * looks up the device's active session via SessionManager at fire
    time (storage device-scoped, delivery session-scoped per A3)
  * enforces the per-device runaway cap of 100 (RFC E2)

Tier 1 contract: when the device has no active session at fire time,
log + drop (mark fired anyway so the row doesn't leak as pending).
ε2 will add an offline queue + boot replay; this manager's
``_fire_one`` provides the seam where that goes.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from dragon_voice.scheduler.models import Notification
from dragon_voice.scheduler.store import NotificationStore

logger = logging.getLogger(__name__)


# Per-device cap on pending notifications.  RFC E2 / R2: hostile LLM
# or buggy caller schedules 1000 reminders by accident → memory blow
# + Tab5 widget_card storm.  100 pending is generous for a real user
# (a person doesn't have 100 active reminders) and tight enough that
# a bug stops fast.
RUNAWAY_CAP_PER_DEVICE = 100


class RunawayCapError(RuntimeError):
    """Raised when a device has hit RUNAWAY_CAP_PER_DEVICE pending
    notifications.  Caller (tool / REST endpoint) catches and
    surfaces a structured error.
    """


class SchedulerManager:
    """Owns the in-process scheduler.

    Constructed once per Dragon process via ``lifecycle/startup.py``
    after SurfaceManager + SessionManager are ready.  Shut down via
    ``lifecycle/shutdown.py`` BEFORE pipeline drain so in-flight
    fires don't race against closed WSes.
    """

    def __init__(
        self,
        *,
        store: NotificationStore,
        surface_mgr,
        session_mgr,
    ) -> None:
        self._store = store
        self._surface_mgr = surface_mgr
        self._session_mgr = session_mgr
        # The hot-path map.  Tasks added by schedule(), removed in
        # _fire_one's finally block + cancel() + reschedule().
        self._tasks: dict[str, asyncio.Task] = {}
        # set in start() — lets ε2 hook boot-replay here without
        # changing the Tier-1 manager's __init__ signature.
        self._started: bool = False

    # ── Lifecycle ──────────────────────────────────────────────────

    async def start(self) -> None:
        """Currently a no-op for ε1a (in-memory store has nothing to
        replay).  ε2 will add boot replay for due-but-unfired
        notifications here."""
        self._started = True
        logger.debug("SchedulerManager.start: ε1a (in-memory) — no boot replay")

    async def shutdown(self) -> None:
        """Cancel every in-flight task + await each so the asyncio
        loop reaps them cleanly.  Pattern matches the cancel-then-await
        discipline in lifecycle/shutdown.py:42-67 (W14-M09 fix).

        After shutdown, no further fires happen even if the loop
        keeps running — the manager is dead.
        """
        tasks_snapshot = list(self._tasks.values())
        for task in tasks_snapshot:
            if not task.done():
                task.cancel()
        for task in tasks_snapshot:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                # Cancellation is the expected outcome; swallow other
                # exceptions to keep shutdown idempotent.
                pass
        self._tasks.clear()
        self._started = False
        logger.info("SchedulerManager shut down (cancelled %d in-flight task(s))",
                    len(tasks_snapshot))

    # ── Scheduling ─────────────────────────────────────────────────

    async def schedule(self, notification: Notification) -> Notification:
        """Persist + start an asyncio task that fires at fire_at.

        Raises ``RunawayCapError`` if the device already has
        RUNAWAY_CAP_PER_DEVICE pending notifications.  This is the
        primary defence against an LLM scheduling 1000 reminders
        in a tight loop (RFC R2).
        """
        if notification.device_id is not None:
            # Device-scoped cap.  REST callers can pass device_id=None
            # for "broadcast" (unused in Tier 1/2 but reserved); those
            # bypass the cap and rely on REST-layer auth + future quota
            # work.  The LLM tool always passes a device_id.
            current = await self._store.count_pending_for_device(
                notification.device_id
            )
            if current >= RUNAWAY_CAP_PER_DEVICE:
                raise RunawayCapError(
                    f"device {notification.device_id} already has "
                    f"{current} pending notifications "
                    f"(cap={RUNAWAY_CAP_PER_DEVICE})"
                )

        await self._store.create(notification)
        self._tasks[notification.id] = asyncio.create_task(
            self._run(notification.id, notification.fire_at)
        )
        logger.info(
            "Scheduled notification %s for device=%s fire_at=%.0f",
            notification.id, notification.device_id, notification.fire_at,
        )
        return notification

    async def cancel(self, notif_id: str) -> bool:
        """Cancel a pending notification.  Idempotent: returns False
        if already fired / cancelled / missing."""
        ok = await self._store.cancel(notif_id)
        if not ok:
            return False
        task = self._tasks.pop(notif_id, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        return True

    async def reschedule(self, notif_id: str, new_fire_at: float) -> bool:
        """Move a pending notification's fire_at.  Replaces the
        in-flight asyncio task with one targeted at the new time.
        Returns False if the row is missing / not pending."""
        ok = await self._store.update_fire_at(notif_id, new_fire_at)
        if not ok:
            return False
        # Cancel old task, spawn new one
        old = self._tasks.pop(notif_id, None)
        if old is not None and not old.done():
            old.cancel()
            try:
                await old
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks[notif_id] = asyncio.create_task(
            self._run(notif_id, new_fire_at)
        )
        return True

    # ── Internal: per-notification task body ───────────────────────

    async def _run(self, notif_id: str, fire_at: float) -> None:
        """Sleep until fire_at, then fire.  Catches CancelledError
        cleanly so cancel + reschedule + shutdown don't propagate."""
        try:
            sleep_for = max(0.0, fire_at - time.time())
            await asyncio.sleep(sleep_for)
            await self._fire_one(notif_id)
        except asyncio.CancelledError:
            # Cancel paths (user cancel, reschedule, shutdown) all
            # land here.  Don't re-raise — caller already updated
            # the store.
            raise
        finally:
            # Remove ourself from the task map even on cancel so the
            # dict doesn't leak references to done tasks.  The
            # `pop(..., None)` guard handles the cancel/reschedule
            # case where the caller already popped us.
            self._tasks.pop(notif_id, None)

    async def _fire_one(self, notif_id: str) -> None:
        """Look up the notification, find the device's active session,
        emit a widget_card via Tab5Surface, mark fired in store.

        ε2 inserts the offline-queue write here when no active
        session exists (instead of just dropping).
        """
        notif = await self._store.get(notif_id)
        if notif is None or notif.status != "pending":
            # Raced with cancel — nothing to do.
            return

        # Find the device's active session.  RFC A3: storage is
        # device-scoped, delivery is session-scoped.
        session = await self._lookup_active_session(notif.device_id)

        if session is None:
            # Tier 1 (RFC R4): log + drop.  Mark fired so it doesn't
            # leak as pending forever.  ε2 will queue for replay.
            logger.warning(
                "Notification %s fired but device %s has no active "
                "session — dropped (Tier 1 limitation)",
                notif_id, notif.device_id,
            )
            await self._store.mark_fired(notif_id)
            return

        # Build the widget_card payload (RFC C.3).
        surface = self._surface_mgr.surface_for(session["id"], "scheduler")
        if surface is None:
            logger.warning(
                "Notification %s fired but no Tab5Surface for session %s — "
                "dropped",
                notif_id, session["id"],
            )
            await self._store.mark_fired(notif_id)
            return

        # Deterministic card_id so the dashboard can correlate to the
        # notification row (RFC C.3).
        card_id = f"sched_{notif_id.replace('sched_', '')[:8]}"

        try:
            await surface.card(
                title=notif.title,
                body=notif.body,
                tone=notif.tone,
                icon="bell",
                action=("Dismiss", "scheduler.dismiss"),
                card_id=card_id,
                skill_id="scheduler",
            )
            logger.info(
                "Notification %s fired → device=%s session=%s",
                notif_id, notif.device_id, session["id"],
            )
        except Exception as e:
            logger.warning(
                "Notification %s fire delivery failed (still marking fired): %s",
                notif_id, e,
            )
        await self._store.mark_fired(notif_id)

    async def _lookup_active_session(
        self, device_id: Optional[str],
    ) -> Optional[dict]:
        """Find the device's currently-active session, or None if the
        device is offline / has no active session."""
        if device_id is None:
            return None
        sessions = await self._session_mgr.list_sessions(
            device_id=device_id,
            status="active",
            limit=1,
        )
        return sessions[0] if sessions else None


__all__ = ["SchedulerManager", "RunawayCapError", "RUNAWAY_CAP_PER_DEVICE"]
