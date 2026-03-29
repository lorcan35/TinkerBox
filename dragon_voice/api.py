"""REST API routes for TinkerClaw (v1).

Provides HTTP endpoints for sessions, messages, devices, and config.
Registered on the aiohttp app by the server module.

refs #21
"""

import json
import logging
from typing import Any

from aiohttp import web

from dragon_voice.db import Database
from dragon_voice.sessions import SessionManager
from dragon_voice.messages import MessageStore

logger = logging.getLogger(__name__)


def _json_error(message: str, status: int = 400) -> web.Response:
    """Return a JSON error response."""
    return web.json_response({"error": message}, status=status)


def _paginated_response(items: list[dict], limit: int, offset: int) -> web.Response:
    """Return a paginated JSON response."""
    return web.json_response({
        "items": items,
        "count": len(items),
        "limit": limit,
        "offset": offset,
    })


class APIRoutes:
    """REST API route handlers for TinkerClaw v1.

    All routes are prefixed with /api/v1/.
    """

    def __init__(
        self,
        db: Database,
        session_mgr: SessionManager,
        message_store: MessageStore,
    ) -> None:
        self._db = db
        self._session_mgr = session_mgr
        self._messages = message_store

    def register(self, app: web.Application) -> None:
        """Register all API routes on the aiohttp app."""
        # Sessions
        app.router.add_get("/api/v1/sessions", self.list_sessions)
        app.router.add_post("/api/v1/sessions", self.create_session)
        app.router.add_get("/api/v1/sessions/{session_id}", self.get_session)
        app.router.add_post("/api/v1/sessions/{session_id}/end", self.end_session)

        # Messages
        app.router.add_get("/api/v1/sessions/{session_id}/messages", self.list_messages)

        # Devices
        app.router.add_get("/api/v1/devices", self.list_devices)
        app.router.add_get("/api/v1/devices/{device_id}", self.get_device)

        # Config
        app.router.add_get("/api/v1/config", self.list_config)
        app.router.add_get("/api/v1/config/{key}", self.get_config)
        app.router.add_put("/api/v1/config/{key}", self.set_config)

        logger.info("API v1 routes registered")

    # ── Sessions ───────────────────────────────────────────────────────

    async def list_sessions(self, request: web.Request) -> web.Response:
        """GET /api/v1/sessions?device_id=&status=&limit=&offset="""
        device_id = request.query.get("device_id")
        status = request.query.get("status")
        limit = min(int(request.query.get("limit", "50")), 200)
        offset = int(request.query.get("offset", "0"))

        sessions = await self._session_mgr.list_sessions(
            device_id=device_id, status=status, limit=limit, offset=offset
        )
        return _paginated_response(sessions, limit, offset)

    async def create_session(self, request: web.Request) -> web.Response:
        """POST /api/v1/sessions {device_id?, type?, system_prompt?}"""
        try:
            body = await request.json()
        except Exception:
            return _json_error("Invalid JSON body")

        session = await self._session_mgr.create_session(
            device_id=body.get("device_id"),
            session_type=body.get("type", "conversation"),
            system_prompt=body.get("system_prompt", ""),
            config=body.get("config"),
        )
        return web.json_response(session, status=201)

    async def get_session(self, request: web.Request) -> web.Response:
        """GET /api/v1/sessions/{session_id}"""
        session_id = request.match_info["session_id"]
        session = await self._session_mgr.get_session(session_id)
        if not session:
            return _json_error("Session not found", 404)
        return web.json_response(session)

    async def end_session(self, request: web.Request) -> web.Response:
        """POST /api/v1/sessions/{session_id}/end"""
        session_id = request.match_info["session_id"]
        session = await self._session_mgr.get_session(session_id)
        if not session:
            return _json_error("Session not found", 404)

        await self._session_mgr.end_session(session_id)
        return web.json_response({"status": "ended", "session_id": session_id})

    # ── Messages ───────────────────────────────────────────────────────

    async def list_messages(self, request: web.Request) -> web.Response:
        """GET /api/v1/sessions/{session_id}/messages?limit=&offset="""
        session_id = request.match_info["session_id"]

        # Verify session exists
        session = await self._session_mgr.get_session(session_id)
        if not session:
            return _json_error("Session not found", 404)

        limit = min(int(request.query.get("limit", "100")), 500)
        offset = int(request.query.get("offset", "0"))

        messages = await self._messages.get_messages(
            session_id, limit=limit, offset=offset
        )
        return _paginated_response(messages, limit, offset)

    # ── Devices ────────────────────────────────────────────────────────

    async def list_devices(self, request: web.Request) -> web.Response:
        """GET /api/v1/devices?online=true"""
        online_only = request.query.get("online", "").lower() in ("true", "1")
        devices = await self._db.list_devices(online_only=online_only)
        return web.json_response({"items": devices, "count": len(devices)})

    async def get_device(self, request: web.Request) -> web.Response:
        """GET /api/v1/devices/{device_id}"""
        device_id = request.match_info["device_id"]
        device = await self._db.get_device(device_id)
        if not device:
            return _json_error("Device not found", 404)
        return web.json_response(device)

    # ── Config Store ───────────────────────────────────────────────────

    async def list_config(self, request: web.Request) -> web.Response:
        """GET /api/v1/config?scope=global&scope_id="""
        scope = request.query.get("scope", "global")
        scope_id = request.query.get("scope_id")
        entries = await self._db.list_config(scope, scope_id)
        return web.json_response({"scope": scope, "scope_id": scope_id, "entries": entries})

    async def get_config(self, request: web.Request) -> web.Response:
        """GET /api/v1/config/{key}?scope=global&scope_id=&resolve=false

        If resolve=true, applies scope resolution: session > device > global.
        """
        key = request.match_info["key"]
        resolve = request.query.get("resolve", "").lower() in ("true", "1")

        if resolve:
            device_id = request.query.get("device_id")
            session_id = request.query.get("session_id")
            value = await self._db.get_resolved_config(key, device_id, session_id)
        else:
            scope = request.query.get("scope", "global")
            scope_id = request.query.get("scope_id")
            value = await self._db.get_config(key, scope, scope_id)

        if value is None:
            return _json_error(f"Config key '{key}' not found", 404)

        return web.json_response({"key": key, "value": json.loads(value)})

    async def set_config(self, request: web.Request) -> web.Response:
        """PUT /api/v1/config/{key} {value, scope?, scope_id?}"""
        key = request.match_info["key"]
        try:
            body = await request.json()
        except Exception:
            return _json_error("Invalid JSON body")

        if "value" not in body:
            return _json_error("'value' field is required")

        scope = body.get("scope", "global")
        scope_id = body.get("scope_id")
        value_json = json.dumps(body["value"])

        await self._db.set_config(key, value_json, scope, scope_id)
        return web.json_response({"key": key, "value": body["value"], "scope": scope})
