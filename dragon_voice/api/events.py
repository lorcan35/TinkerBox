"""Events listing API routes."""

import logging

from aiohttp import web

from dragon_voice.db import Database

logger = logging.getLogger(__name__)


class EventRoutes:
    def __init__(self, db: Database) -> None:
        self._db = db

    def register(self, app: web.Application) -> None:
        app.router.add_get("/api/v1/events", self.list_events)

    async def list_events(self, request: web.Request) -> web.Response:
        """GET /api/v1/events?since_id=0&type=&session_id=&device_id=&limit=50"""
        since_id = int(request.query.get("since_id", "0"))
        event_type = request.query.get("type")
        session_id = request.query.get("session_id")
        device_id = request.query.get("device_id")
        limit = min(int(request.query.get("limit", "50")), 200)

        events = await self._db.get_events(
            event_type=event_type,
            session_id=session_id,
            device_id=device_id,
            since_id=since_id,
            limit=limit,
        )
        return web.json_response({"items": events, "count": len(events)})
