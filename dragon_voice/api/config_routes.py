"""Config store CRUD API routes."""

import json
import logging

from aiohttp import web

from dragon_voice.api.utils import json_error, parse_json_body
from dragon_voice.db import Database

logger = logging.getLogger(__name__)


class ConfigRoutes:
    def __init__(self, db: Database) -> None:
        self._db = db

    def register(self, app: web.Application) -> None:
        app.router.add_get("/api/v1/config", self.list_config)
        app.router.add_get("/api/v1/config/{key}", self.get_config)
        app.router.add_put("/api/v1/config/{key}", self.set_config)
        # Sprint 1: delete
        app.router.add_delete("/api/v1/config/{key}", self.delete_config)

    async def list_config(self, request: web.Request) -> web.Response:
        """GET /api/v1/config?scope=global&scope_id="""
        scope = request.query.get("scope", "global")
        scope_id = request.query.get("scope_id")
        entries = await self._db.list_config(scope, scope_id)
        return web.json_response({"scope": scope, "scope_id": scope_id, "entries": entries})

    async def get_config(self, request: web.Request) -> web.Response:
        """GET /api/v1/config/{key}?scope=global&scope_id=&resolve=false"""
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
            return json_error(f"Config key '{key}' not found", 404)
        return web.json_response({"key": key, "value": json.loads(value)})

    async def set_config(self, request: web.Request) -> web.Response:
        """PUT /api/v1/config/{key}"""
        key = request.match_info["key"]
        body, err = await parse_json_body(request)
        if err:
            return err
        if "value" not in body:
            return json_error("'value' field is required")
        scope = body.get("scope", "global")
        scope_id = body.get("scope_id")
        value_json = json.dumps(body["value"])
        await self._db.set_config(key, value_json, scope, scope_id)
        return web.json_response({"key": key, "value": body["value"], "scope": scope})

    async def delete_config(self, request: web.Request) -> web.Response:
        """DELETE /api/v1/config/{key}?scope=global&scope_id="""
        key = request.match_info["key"]
        scope = request.query.get("scope", "global")
        scope_id = request.query.get("scope_id")
        deleted = await self._db.delete_config(key, scope, scope_id)
        if not deleted:
            return json_error(f"Config key '{key}' not found", 404)
        return web.json_response({"status": "deleted", "key": key, "scope": scope})
