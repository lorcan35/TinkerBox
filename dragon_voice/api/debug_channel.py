"""POST /api/v1/debug/channel_message — push a synthetic channel_message frame.

W7-F stub (closes round-trip with TT #471 / W7-E.4b on the Tab5 side).
Real channel arrivals (Telegram, WhatsApp, etc.) will eventually come
from W7-F.2's WS-RPC client to the OpenClaw gateway.  This endpoint
lets us test the Tab5 notification surface end-to-end without that
plumbing — Dragon receives the synthetic payload via REST and fans it
out to the addressed Tab5 over the existing voice WS.

Body shape matches what Tab5 expects (see TT
docs/PLAN-agent-mode-notification-surface.md §6):

    {
      "channel": "tg",
      "message_id": "tg:demo:1",
      "thread_id": "tg:demo",
      "sender": {"display_name": "Mom", "starred": true},
      "text": "Are you free Sunday?",
      "preview": "Are you free Sunday?",
      "priority": "high",
      "needs_reply": true
    }

Query: ?device_id=X  OR  ?session_id=Y  identifies the target connection.

Bearer-authed (the global auth middleware gates anything under
/api/v1/* except the explicitly-public prefixes).
"""

from __future__ import annotations

import logging
from typing import Callable

from aiohttp import web

logger = logging.getLogger(__name__)


class DebugChannelRoutes:
    """REST routes that fan synthetic channel_message frames to a Tab5.

    Mirrors video_inject.py's structure: caller supplies
    ``get_active_connections`` so the route doesn't import the
    VoiceServer class directly.
    """

    def __init__(self, get_active_connections: Callable[[], dict]) -> None:
        self._get_conns = get_active_connections

    def register(self, app: web.Application) -> None:
        app.router.add_post("/api/v1/debug/channel_message", self.push)

    async def push(self, request: web.Request) -> web.Response:
        target_session = request.query.get("session_id")
        target_device = request.query.get("device_id")
        if not target_session and not target_device:
            return web.json_response(
                {"error": "session_id or device_id required"}, status=400
            )

        try:
            payload = await request.json()
        except ValueError:
            return web.json_response({"error": "invalid JSON body"}, status=400)

        if not isinstance(payload, dict):
            return web.json_response({"error": "body must be a JSON object"}, status=400)

        # Required: channel.  Everything else has Tab5-side defaults.
        if not payload.get("channel"):
            return web.json_response({"error": "channel required"}, status=400)

        # Force the WS frame type so callers can't accidentally push
        # an arbitrary frame shape through this endpoint.
        frame = dict(payload)
        frame["type"] = "channel_message"

        # Resolve target connection (same scan video_inject uses).
        conns = self._get_conns()
        match = None
        for conn in conns.values():
            sid = conn.get("session_id", "")
            did = conn.get("device_id", "")
            if target_session and sid == target_session:
                match = conn
                break
            if target_device and did == target_device:
                match = conn
                break

        if not match:
            return web.json_response(
                {
                    "error": "session not connected",
                    "session_id": target_session,
                    "device_id": target_device,
                },
                status=404,
            )

        ws = match.get("ws")
        if ws is None or ws.closed:
            return web.json_response({"error": "ws closed"}, status=410)

        try:
            await ws.send_json(frame)
        except Exception as e:
            logger.warning("debug_channel push send_json failed: %s", e)
            return web.json_response({"error": f"send failed: {e}"}, status=502)

        logger.info(
            "debug_channel push: ch=%s sender=%s pri=%s → device=%s session=%s",
            frame.get("channel"),
            (frame.get("sender") or {}).get("display_name") if isinstance(frame.get("sender"), dict) else "",
            frame.get("priority", ""),
            match.get("device_id", ""),
            match.get("session_id", ""),
        )

        return web.json_response(
            {
                "sent": True,
                "channel": frame.get("channel"),
                "device_id": match.get("device_id", ""),
                "session_id": match.get("session_id", ""),
            }
        )
