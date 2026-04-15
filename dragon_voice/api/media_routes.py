"""Media file serving and upload routes for Dragon Voice Server.

Endpoints:
    GET  /api/media/{media_id}  — serve a stored media file to Tab5
    POST /api/media/upload      — accept a BMP/JPEG camera frame from Tab5
"""

import io
import logging

from aiohttp import web

from dragon_voice.media.store import MediaStore

logger = logging.getLogger(__name__)

_CONTENT_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "wav": "audio/wav",
}

_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB hard cap
_JPEG_MAX_PX = 1280
_JPEG_QUALITY = 85


class MediaRoutes:
    def __init__(self, media_store: MediaStore) -> None:
        self._store = media_store

    def register(self, app: web.Application) -> None:
        app.router.add_get("/api/media/{media_id}", self.serve_media)
        app.router.add_post("/api/media/upload", self.upload_media)

    # ── Handlers ────────────────────────────────────────────────────────

    async def serve_media(self, request: web.Request) -> web.Response:
        """GET /api/media/{media_id} — stream a stored media file."""
        media_id = request.match_info["media_id"]
        path = await self._store.get_path(media_id)

        if path is None:
            return web.Response(text="Not found", status=404)

        # Derive content-type from extension
        ext = media_id.rsplit(".", 1)[-1].lower() if "." in media_id else ""
        content_type = _CONTENT_TYPES.get(ext, "application/octet-stream")

        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            logger.warning("serve_media: could not read %s: %s", path, exc)
            return web.Response(text="Not found", status=404)

        return web.Response(
            body=data,
            content_type=content_type,
            headers={"Cache-Control": "max-age=3600"},
        )

    async def upload_media(self, request: web.Request) -> web.Response:
        """POST /api/media/upload — accept BMP or JPEG from Tab5 camera.

        Converts the image to JPEG (max 1280px longest side, RGB, quality 85)
        and stores it via MediaStore.  Returns ``{"media_id": "..."}``.
        """
        try:
            from PIL import Image
        except ImportError:
            logger.error("upload_media: Pillow is not installed")
            return web.Response(
                text='{"error":"Pillow not installed"}',
                status=500,
                content_type="application/json",
            )

        raw = await request.read()
        if not raw:
            return web.json_response({"error": "empty body"}, status=400)
        if len(raw) > _MAX_UPLOAD_BYTES:
            return web.json_response({"error": "payload too large"}, status=413)

        try:
            img = Image.open(io.BytesIO(raw))
        except Exception as exc:
            logger.warning("upload_media: could not decode image: %s", exc)
            return web.json_response({"error": "invalid image"}, status=400)

        # Resize so the longest side is at most _JPEG_MAX_PX
        w, h = img.size
        if max(w, h) > _JPEG_MAX_PX:
            scale = _JPEG_MAX_PX / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

        # Ensure RGB (BMP frames may be RGBA or palette-mode)
        if img.mode != "RGB":
            img = img.convert("RGB")

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_QUALITY)
        jpeg_bytes = buf.getvalue()

        session_id = request.headers.get("X-Session-Id", "")
        media_id = await self._store.store(jpeg_bytes, "jpg", session_id=session_id)
        logger.debug("upload_media: stored %s (%d bytes)", media_id, len(jpeg_bytes))

        return web.json_response({"media_id": media_id})
