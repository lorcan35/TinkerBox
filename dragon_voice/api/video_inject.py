"""POST /api/video/inject — debug endpoint for Phase 3B video downlink.

Wraps a JPEG payload in the same wire format Tab5 uses for the uplink
(\"VID0\" + 4-byte BE length + JPEG bytes) and pushes it as a binary WS
frame to the connected Tab5 identified by session_id (or device_id).

Phase 3C will replace this debug endpoint with a relay queue between
two paired Tab5s; this exists to validate Tab5's downlink decode +
ui_video_pane render path without needing a second client.
"""

from __future__ import annotations

import logging
import struct
from typing import Callable

from aiohttp import web

logger = logging.getLogger(__name__)

VIDEO_MAGIC = b"VID0"
VIDEO_MAX_PAYLOAD = 96 * 1024   # match Tab5 voice_video.c slot ceiling


def _wrap_video_frame(jpeg: bytes) -> bytes:
    """Apply the on-wire framing: magic + 4-byte BE length + payload."""
    return VIDEO_MAGIC + struct.pack(">I", len(jpeg)) + jpeg


class VideoInjectRoutes:
    """REST routes that push video frames to a connected Tab5.

    The route handler resolves a session_id (or device_id) to an open
    WebSocket via `get_active_connections()` (the same accessor the
    System routes use) and sends the framed bytes via ws.send_bytes.
    """

    def __init__(self, get_active_connections: Callable[[], dict]) -> None:
        self._get_conns = get_active_connections

    def register(self, app: web.Application) -> None:
        app.router.add_post("/api/video/inject", self.inject)

    async def inject(self, request: web.Request) -> web.Response:
        target_session = request.query.get("session_id")
        target_device  = request.query.get("device_id")
        if not target_session and not target_device:
            return web.json_response(
                {"error": "session_id or device_id required"}, status=400,
            )

        body = await request.read()
        if not body:
            return web.json_response({"error": "empty body"}, status=400)
        if len(body) > VIDEO_MAX_PAYLOAD:
            return web.json_response(
                {"error": f"payload too large ({len(body)} > {VIDEO_MAX_PAYLOAD})"},
                status=413,
            )

        # Find the matching connection.  Active connections are keyed
        # by ws_id; we scan because session/device counts are tiny.
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
                {"error": "session not connected",
                 "session_id": target_session, "device_id": target_device},
                status=404,
            )

        ws = match.get("ws")
        if ws is None or ws.closed:
            return web.json_response({"error": "ws closed"}, status=410)

        wire = _wrap_video_frame(body)
        try:
            await ws.send_bytes(wire)
        except Exception as e:
            logger.warning("video_inject send_bytes failed: %s", e)
            return web.json_response({"error": f"send failed: {e}"}, status=502)

        return web.json_response({
            "sent":   True,
            "bytes":  len(wire),
            "jpeg":   len(body),
        })
