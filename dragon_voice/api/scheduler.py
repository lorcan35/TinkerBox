"""REST API routes for the scheduler.

Phase 5 ε1b (refs #126).  See docs/RFC-scheduler.md C.4 for the
endpoint contract + request/response bodies.

Five endpoints, mirroring the /api/v1/sessions + /api/v1/memory
shape exactly:

  POST   /api/v1/scheduler/notifications        — create
  GET    /api/v1/scheduler/notifications        — list (filter by device_id, status)
  GET    /api/v1/scheduler/notifications/{id}   — get one
  DELETE /api/v1/scheduler/notifications/{id}   — cancel
  PATCH  /api/v1/scheduler/notifications/{id}   — reschedule (when only)

Naming is deliberate: ``notifications`` not ``reminders``.  Future
use cases (deploy notifier, weather alert, calendar pop) are
notifications too — calling it ``/reminders`` would either misname
them or force a parallel ``/api/v1/scheduler/alerts`` later.

REST callers (dashboard, curl) MUST pass ``device_id`` explicitly
on POST.  Tool callers (LLM) inherit it from the active session
inside ScheduleReminderTool — REST doesn't have that context.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Optional

from aiohttp import web

from dragon_voice.api.utils import (
    json_error,
    paginated_response,
    parse_json_body,
    parse_pagination,
)
from dragon_voice.scheduler.manager import (
    RUNAWAY_CAP_PER_DEVICE,
    RunawayCapError,
    SchedulerManager,
)
from dragon_voice.scheduler.models import Notification
from dragon_voice.scheduler.parser import parse_when

logger = logging.getLogger(__name__)


# Tone mapping mirrors the LLM tool's mapping so REST and LLM
# callers get identical semantics.  Pin here so divergence is
# a deliberate two-line change, not silent drift.
_PRIORITY_TONE = {
    "normal": "info",
    "important": "warn",
}


class SchedulerRoutes:
    def __init__(self, scheduler_mgr: SchedulerManager) -> None:
        self._mgr = scheduler_mgr

    def register(self, app: web.Application) -> None:
        app.router.add_post(
            "/api/v1/scheduler/notifications", self.create_notification
        )
        app.router.add_get(
            "/api/v1/scheduler/notifications", self.list_notifications
        )
        app.router.add_get(
            "/api/v1/scheduler/notifications/{notif_id}",
            self.get_notification,
        )
        app.router.add_delete(
            "/api/v1/scheduler/notifications/{notif_id}",
            self.cancel_notification,
        )
        app.router.add_patch(
            "/api/v1/scheduler/notifications/{notif_id}",
            self.reschedule_notification,
        )

    # ── handlers ───────────────────────────────────────────────────

    async def create_notification(self, request: web.Request) -> web.Response:
        """POST /api/v1/scheduler/notifications

        Body: {when, message, title?, priority?, device_id}
        """
        body, err = await parse_json_body(request)
        if err:
            return err

        when_str = (body.get("when") or "").strip()
        message = (body.get("message") or "").strip()
        device_id = (body.get("device_id") or "").strip() or None
        title = (body.get("title") or "Reminder").strip()
        priority = (body.get("priority") or "normal").strip().lower()

        if not when_str:
            return json_error("'when' is required")
        if not message:
            return json_error("'message' is required")
        if not device_id:
            # REST callers MUST identify the target device.  The LLM
            # tool inherits device_id from the session; REST doesn't
            # have that fallback so reject loudly.
            return json_error("'device_id' is required")

        now = time.time()
        local_tz = datetime.now().astimezone().tzinfo
        try:
            fire_at = parse_when(when_str, now=now, tz=local_tz)
        except ValueError as e:
            return web.json_response(
                {
                    "error": f"Invalid 'when' value: {e}",
                    "code": "scheduler_when_parse",
                },
                status=400,
            )

        notif = Notification(
            device_id=device_id,
            fire_at=fire_at,
            title=title,
            body=message,
            tone=_PRIORITY_TONE.get(priority, "info"),
        )

        try:
            scheduled = await self._mgr.schedule(notif)
        except RunawayCapError as e:
            logger.warning("REST scheduler runaway cap hit: %s", e)
            return web.json_response(
                {
                    "error": (
                        f"Too many pending reminders for device "
                        f"{device_id!r} (cap={RUNAWAY_CAP_PER_DEVICE})."
                    ),
                    "code": "scheduler_runaway_cap",
                },
                status=429,
            )

        return web.json_response(_serialize(scheduled, local_tz), status=201)

    async def list_notifications(self, request: web.Request) -> web.Response:
        """GET /api/v1/scheduler/notifications?device_id=...&status=pending"""
        device_id = request.query.get("device_id") or None
        status = request.query.get("status") or "pending"
        limit, offset = parse_pagination(request)

        notifs = await self._mgr._store.list_all(
            device_id=device_id, status=status,
        )
        # In-memory pagination is fine here — the cap of 100 pending
        # per device + low historical volume means the list is small.
        # ε2's SqliteNotificationStore can push pagination into SQL.
        page = notifs[offset:offset + limit]
        local_tz = datetime.now().astimezone().tzinfo
        return paginated_response(
            [_serialize(n, local_tz) for n in page],
            limit, offset,
        )

    async def get_notification(self, request: web.Request) -> web.Response:
        """GET /api/v1/scheduler/notifications/{notif_id}"""
        notif_id = request.match_info["notif_id"]
        notif = await self._mgr._store.get(notif_id)
        if notif is None:
            return json_error("Notification not found", 404)
        local_tz = datetime.now().astimezone().tzinfo
        return web.json_response(_serialize(notif, local_tz))

    async def cancel_notification(self, request: web.Request) -> web.Response:
        """DELETE /api/v1/scheduler/notifications/{notif_id}"""
        notif_id = request.match_info["notif_id"]
        ok = await self._mgr.cancel(notif_id)
        if not ok:
            return json_error("Notification not found or already complete", 404)
        return web.json_response({"status": "cancelled", "id": notif_id})

    async def reschedule_notification(self, request: web.Request) -> web.Response:
        """PATCH /api/v1/scheduler/notifications/{notif_id}

        Body: {when: "10m"} — only ``when`` is patchable in Tier 1.
        """
        notif_id = request.match_info["notif_id"]
        notif = await self._mgr._store.get(notif_id)
        if notif is None:
            return json_error("Notification not found", 404)
        if notif.status != "pending":
            return json_error(
                f"Cannot reschedule a {notif.status} notification", 400,
            )

        body, err = await parse_json_body(request)
        if err:
            return err
        when_str = (body.get("when") or "").strip()
        if not when_str:
            return json_error("'when' is required")

        now = time.time()
        local_tz = datetime.now().astimezone().tzinfo
        try:
            new_fire_at = parse_when(when_str, now=now, tz=local_tz)
        except ValueError as e:
            return web.json_response(
                {
                    "error": f"Invalid 'when' value: {e}",
                    "code": "scheduler_when_parse",
                },
                status=400,
            )

        ok = await self._mgr.reschedule(notif_id, new_fire_at)
        if not ok:
            # Race: notification was cancelled / fired between our
            # get() check and the reschedule() call.  Surface as 409
            # so the caller knows to refresh state.
            return json_error("Notification state changed mid-request", 409)

        # Re-fetch to get the canonical post-reschedule shape.
        updated = await self._mgr._store.get(notif_id)
        return web.json_response(_serialize(updated, local_tz))


# ───────────────────────── helpers


def _serialize(notif: Notification, tz) -> dict:
    """Map Notification dataclass → JSON dict for the wire.

    Adds ``fires_at_iso`` for client-side display so consumers don't
    have to do timezone math in JS.
    """
    fires_at_iso: Optional[str] = None
    if notif.fire_at:
        try:
            fires_at_iso = datetime.fromtimestamp(
                notif.fire_at, tz=tz,
            ).isoformat()
        except (OSError, OverflowError, ValueError):
            fires_at_iso = None
    return {
        "id": notif.id,
        "device_id": notif.device_id,
        "originating_session_id": notif.originating_session_id,
        "fire_at": notif.fire_at,
        "fires_at_iso": fires_at_iso,
        "title": notif.title,
        "body": notif.body,
        "tone": notif.tone,
        "status": notif.status,
        "recurrence": notif.recurrence,
        "created_at": notif.created_at,
        "fired_at": notif.fired_at,
        "cancelled_at": notif.cancelled_at,
    }


__all__ = ["SchedulerRoutes"]
