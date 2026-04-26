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

# ε2 boot-replay window (RFC R8): notifications whose fire_at is
# more than this many seconds in the past are NOT replayed at boot.
# Marked `failed` instead — surfacing a 4-hour-old "remind me to
# take out the trash" after a long Dragon outage is worse than no
# reminder at all.
REPLAY_WINDOW_SECONDS = 15 * 60  # 15 minutes

# ε2 offline-queue replay pacing (RFC R5): time between consecutive
# widget_card frames when draining a device's offline queue.  Slow
# enough that Tab5's WS receive buffer doesn't fill up, fast enough
# that a 50-frame replay drains in 5 seconds.
REPLAY_PACING_SECONDS = 0.1


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
        """Boot-time replay of all pending notifications.

        ε2 (RFC R8): walks the store's `list_pending()` and for each:
          * fire_at FUTURE (still pending, just need to recreate the
            asyncio task that died with the prior process) → schedule
            an in-flight task targeted at the original fire_at
          * fire_at PAST + within REPLAY_WINDOW_SECONDS → reschedule
            for "fire ASAP" (the _run sleep() will return immediately
            for past fire_at)
          * fire_at PAST + beyond REPLAY_WINDOW_SECONDS → mark
            `failed` (RFC R8 — a 4-hour-old reminder firing now is
            worse than no reminder)

        InMemoryNotificationStore's list_pending returns [] when
        the store is fresh (Tier 1 nothing-survives-boot semantics)
        so this is effectively a no-op for ε1a deployments.
        SqliteNotificationStore returns the real backlog for ε2 —
        the headline durability win.
        """
        self._started = True
        now = time.time()
        try:
            pending = await self._store.list_pending()
        except Exception as e:
            logger.warning(
                "SchedulerManager.start boot-replay query failed: %s", e,
            )
            pending = []

        if not pending:
            logger.info("SchedulerManager started (no pending notifications to replay)")
            return

        rescheduled_future = 0
        replayed_past = 0
        expired = 0
        for notif in pending:
            age_seconds = now - notif.fire_at
            if age_seconds > REPLAY_WINDOW_SECONDS:
                # RFC R8: too stale to fire — mark failed (distinct
                # from cancelled because the user didn't ask, and
                # distinct from fired because the user never saw it).
                logger.warning(
                    "Boot replay: notification %s expired "
                    "(age=%.0fs > window=%ds) — marking failed",
                    notif.id, age_seconds, REPLAY_WINDOW_SECONDS,
                )
                await self._store.mark_failed(notif.id)
                expired += 1
                continue

            if notif.fire_at <= now:
                # Past-due but within window → fire ASAP.
                self._tasks[notif.id] = asyncio.create_task(
                    self._run(notif.id, now + 0.001)
                )
                replayed_past += 1
            else:
                # Future-pending → recreate the in-flight task
                # targeted at the ORIGINAL fire_at.  This is the
                # common case after a routine Dragon restart while
                # reminders are pending.
                self._tasks[notif.id] = asyncio.create_task(
                    self._run(notif.id, notif.fire_at)
                )
                rescheduled_future += 1

        logger.info(
            "SchedulerManager started — boot replay: %d future-pending "
            "rescheduled, %d past-due replayed, %d expired (window=%ds)",
            rescheduled_future, replayed_past, expired, REPLAY_WINDOW_SECONDS,
        )

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

        # Build the rendered widget_card payload up-front — used by
        # both the live-deliver path and the offline-queue path.
        # Doing it once here means the queue stores the same shape
        # the live path sends (RFC B.5: "queued items are post-fire,
        # state has already advanced").
        card_id = f"sched_{notif_id.replace('sched_', '')[:8]}"
        rendered_payload = {
            "type": "widget_card",
            "skill_id": "scheduler",
            "card_id": card_id,
            "title": notif.title,
            "body": notif.body,
            "tone": notif.tone,
            "icon": "bell",
            "action": {"label": "Dismiss", "event": "scheduler.dismiss"},
        }

        if session is None:
            # ε2 (RFC R4): no active session → queue for replay on
            # next register.  The store decides whether to actually
            # persist (SqliteNotificationStore does; the in-memory
            # store no-ops since Tier 1 has no durability to back
            # the queue).  Either way mark fired so the row doesn't
            # leak as pending forever.
            if notif.device_id is not None:
                try:
                    await self._store.queue_notification(
                        notif.device_id, notif_id, rendered_payload,
                    )
                    logger.info(
                        "Notification %s fired but device %s offline — "
                        "queued for replay",
                        notif_id, notif.device_id,
                    )
                except Exception as e:
                    logger.warning(
                        "Notification %s queue write failed: %s "
                        "(falling back to drop)",
                        notif_id, e,
                    )
            else:
                logger.warning(
                    "Notification %s has no device_id — cannot queue, "
                    "dropping", notif_id,
                )
            await self._store.mark_fired(notif_id)
            return

        # Live-deliver path: device has an active session.  Build
        # the Tab5Surface for the scheduler skill_id and call .card()
        # — same widget shape we'd have queued, but sent live.
        surface = self._surface_mgr.surface_for(session["id"], "scheduler")
        if surface is None:
            # Surface missing despite an active session — race with
            # session unregister.  Queue as a fallback so the user
            # still gets the reminder when they come back.
            logger.warning(
                "Notification %s: active session %s but no Tab5Surface "
                "— queuing as fallback",
                notif_id, session["id"],
            )
            if notif.device_id is not None:
                try:
                    await self._store.queue_notification(
                        notif.device_id, notif_id, rendered_payload,
                    )
                except Exception as e:
                    logger.warning("queue fallback also failed: %s", e)
            await self._store.mark_fired(notif_id)
            return

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

    # ── ε2 — replay queued notifications on device register ────────

    async def replay_queued_for_device(self, device_id: str) -> int:
        """Drain the device's offline-queue + send each frame to the
        device's currently-active session.  Called by server.py's
        register hook after a Tab5 reconnect.

        Returns the number of frames replayed.  Paces between frames
        at REPLAY_PACING_SECONDS (RFC R5) so a 50-frame queue
        doesn't slam Tab5's WS receive buffer.

        Safe to call when:
          * the queue is empty (returns 0, no surface lookup)
          * the device has no active session (logs + returns 0;
            frames stay queued for the next register)
          * the surface_mgr surface is missing (same as above)
        """
        try:
            payloads = await self._store.drain_queue_for_device(device_id)
        except Exception as e:
            logger.warning(
                "replay_queued_for_device drain failed for %s: %s",
                device_id, e,
            )
            return 0

        if not payloads:
            return 0

        # Look up the session NOW that we know there are frames to
        # send.  If no active session, leave the frames queued —
        # but drain_queue_for_device already deleted them, so we'd
        # lose them.  Re-queue as a defensive fallback.
        session = await self._lookup_active_session(device_id)
        if session is None:
            logger.warning(
                "replay_queued_for_device: drained %d frames for "
                "device %s but no active session — re-queuing",
                len(payloads), device_id,
            )
            for payload in payloads:
                try:
                    await self._store.queue_notification(
                        device_id, None, payload,
                    )
                except Exception:
                    logger.debug("re-queue failed", exc_info=True)
            return 0

        surface = self._surface_mgr.surface_for(session["id"], "scheduler")
        if surface is None:
            logger.warning(
                "replay_queued_for_device: no Tab5Surface for session "
                "%s — re-queuing %d frames", session["id"], len(payloads),
            )
            for payload in payloads:
                try:
                    await self._store.queue_notification(
                        device_id, None, payload,
                    )
                except Exception:
                    logger.debug("re-queue failed", exc_info=True)
            return 0

        # Deliver each frame, pacing between them.  Use the public
        # surface.card() API so the replay path reuses the same
        # swallow logic + send pipeline as the live-fire path —
        # diverging would mean two slightly-different shapes hitting
        # Tab5 depending on whether the device was online at fire time.
        sent = 0
        for i, payload in enumerate(payloads):
            if i > 0:
                await asyncio.sleep(REPLAY_PACING_SECONDS)
            try:
                action = payload.get("action") or {}
                action_tuple = None
                if action.get("label") and action.get("event"):
                    action_tuple = (action["label"], action["event"])
                await surface.card(
                    title=payload.get("title", "Reminder"),
                    body=payload.get("body", ""),
                    tone=payload.get("tone", "info"),
                    icon=payload.get("icon"),
                    image_url=payload.get("image_url"),
                    action=action_tuple,
                    card_id=payload.get("card_id"),
                    skill_id=payload.get("skill_id", "scheduler"),
                )
                sent += 1
            except Exception as e:
                logger.warning(
                    "replay frame send failed for %s: %s "
                    "(continuing with remaining)",
                    device_id, e,
                )

        logger.info(
            "replay_queued_for_device: delivered %d/%d queued "
            "notification(s) to device %s",
            sent, len(payloads), device_id,
        )
        return sent

    # ── ε2 — snooze action handler ─────────────────────────────────

    async def handle_snooze(
        self, notif_id: str, *, snooze_minutes: int = 10,
    ) -> bool:
        """Reschedule a notification N minutes into the future.
        Returns False if the notification is missing / non-pending.

        Wired up via SurfaceManager.register_action when the action
        event "scheduler.snooze_<N>" fires.  The N is parsed by
        whatever code calls handle_snooze.
        """
        if snooze_minutes <= 0:
            return False
        new_fire = time.time() + snooze_minutes * 60
        return await self.reschedule(notif_id, new_fire)

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
