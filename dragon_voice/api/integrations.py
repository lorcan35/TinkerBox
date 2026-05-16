"""REST API routes for integrations (#341 / #342).

Tab5's Settings → Integrations surface calls these:

  * GET  /api/v1/integrations                     — list + connection state
  * POST /api/v1/integrations/{name}/connect      — start auth flow
  * GET  /api/v1/integrations/{name}/status/{request_id}  — poll flow
  * POST /api/v1/integrations/{name}/disconnect   — revoke + delete
  * GET  /api/v1/integrations/{name}/test         — smoke test

All routes require bearer auth (the standard middleware covers it; no
extra check here).  Errors are surfaced as JSON with a sensible HTTP
status — Tab5 turns them into modal text.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from dragon_voice.api.utils import json_error, parse_json_body
from dragon_voice.tools.integrations import (
    IntegrationBackend,
    IntegrationState,
    list_integrations,
)
from dragon_voice.tools.integrations.oauth import DeviceCodeError
from dragon_voice.tools.integrations.registry import (
    create_integration,
    is_registered,
)

logger = logging.getLogger(__name__)


class IntegrationRoutes:
    """Registers REST routes + holds one instance per integration so
    in-flight auth flows survive across requests."""

    def __init__(self) -> None:
        # name -> backend instance (per-process singleton)
        self._instances: dict[str, IntegrationBackend] = {}

    def _get(self, name: str) -> IntegrationBackend:
        key = (name or "").strip().lower()
        if not is_registered(key):
            raise KeyError(key)
        if key not in self._instances:
            self._instances[key] = create_integration(key)
        return self._instances[key]

    def register(self, app: web.Application) -> None:
        app.router.add_get(
            "/api/v1/integrations",
            self.list_all,
        )
        app.router.add_post(
            "/api/v1/integrations/{name}/connect",
            self.connect,
        )
        app.router.add_get(
            "/api/v1/integrations/{name}/status/{request_id}",
            self.status,
        )
        app.router.add_post(
            "/api/v1/integrations/{name}/disconnect",
            self.disconnect,
        )
        app.router.add_get(
            "/api/v1/integrations/{name}/test",
            self.test,
        )

    # ── handlers ────────────────────────────────────────────────

    async def list_all(self, request: web.Request) -> web.Response:
        del request  # unused
        out: list[dict[str, Any]] = []
        for name in list_integrations():
            integ = self._get(name)
            connected = await integ.is_connected()
            state = (
                IntegrationState.CONNECTED.value
                if connected else IntegrationState.DISCONNECTED.value
            )
            out.append({
                "name": integ.name,
                "display_name": integ.display_name,
                "description": integ.description,
                "auth_kind": integ.auth_kind,
                "state": state,
            })
        return web.json_response({"integrations": out})

    async def connect(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        try:
            integ = self._get(name)
        except KeyError:
            return json_error(f"Unknown integration '{name}'", 404)
        body: dict[str, Any] = {}
        if request.content_length and request.content_length > 0:
            body, err = await parse_json_body(request)
            if err:
                return err
        try:
            chal = await integ.start_connect(params=body or None)
        except DeviceCodeError as e:
            status = 503 if e.code == "oauth_not_configured" else 502
            return web.json_response(
                {"error": e.code, "message": e.description},
                status=status,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("integration %s connect failed", name)
            return json_error(f"Connect failed: {e}", 500)
        return web.json_response({
            "kind": chal.kind,
            "request_id": chal.request_id,
            "verification_url": chal.verification_url,
            "user_code": chal.user_code,
            "expires_in_s": chal.expires_in_s,
            "interval_s": chal.interval_s,
            "prompt_fields": chal.prompt_fields,
        })

    async def status(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        request_id = request.match_info["request_id"]
        try:
            integ = self._get(name)
        except KeyError:
            return json_error(f"Unknown integration '{name}'", 404)
        try:
            status = await integ.poll_status(request_id)
        except Exception as e:  # noqa: BLE001
            logger.exception("integration %s poll_status failed", name)
            return json_error(f"poll_status failed: {e}", 500)
        return web.json_response({
            "state": status.state,
            "request_id": status.request_id,
            "error": status.error,
        })

    async def disconnect(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        try:
            integ = self._get(name)
        except KeyError:
            return json_error(f"Unknown integration '{name}'", 404)
        try:
            await integ.disconnect()
        except Exception as e:  # noqa: BLE001
            logger.exception("integration %s disconnect failed", name)
            return json_error(f"Disconnect failed: {e}", 500)
        return web.json_response({"disconnected": True, "name": name})

    async def test(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        try:
            integ = self._get(name)
        except KeyError:
            return json_error(f"Unknown integration '{name}'", 404)
        ok, detail = await integ.health_check()
        return web.json_response({
            "ok": ok,
            "detail": detail,
            "name": name,
        })
