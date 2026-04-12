"""Device listing and CRUD API routes."""

import logging

from aiohttp import web

from dragon_voice.api.utils import json_error, parse_json_body
from dragon_voice.db import Database

logger = logging.getLogger(__name__)


class DeviceRoutes:
    def __init__(self, db: Database) -> None:
        self._db = db

    def register(self, app: web.Application) -> None:
        app.router.add_get("/api/v1/devices", self.list_devices)
        app.router.add_get("/api/v1/devices/{device_id}", self.get_device)
        # Sprint 1: new CRUD
        app.router.add_patch("/api/v1/devices/{device_id}", self.update_device)
        app.router.add_delete("/api/v1/devices/{device_id}", self.delete_device)

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
            return json_error("Device not found", 404)
        return web.json_response(device)

    async def update_device(self, request: web.Request) -> web.Response:
        """PATCH /api/v1/devices/{device_id} — update name/config"""
        device_id = request.match_info["device_id"]
        device = await self._db.get_device(device_id)
        if not device:
            return json_error("Device not found", 404)

        body, err = await parse_json_body(request)
        if err:
            return err

        allowed = {"name", "config"}
        updates = {k: v for k, v in body.items() if k in allowed}
        if not updates:
            return json_error("No valid fields to update (allowed: name, config)")

        await self._db.update_device(device_id, **updates)
        updated = await self._db.get_device(device_id)
        return web.json_response(updated)

    async def delete_device(self, request: web.Request) -> web.Response:
        """DELETE /api/v1/devices/{device_id}"""
        device_id = request.match_info["device_id"]
        device = await self._db.get_device(device_id)
        if not device:
            return json_error("Device not found", 404)
        await self._db.delete_device(device_id)
        return web.json_response({"status": "deleted", "device_id": device_id})
