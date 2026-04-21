"""Media file serving and upload routes for Dragon Voice Server.

Endpoints:
    GET  /api/media/{media_id}  — serve a stored media file to Tab5
    POST /api/media/upload      — accept a BMP/JPEG camera frame from Tab5
"""

import io
import logging
from typing import Optional

from aiohttp import web

from dragon_voice.media.store import MediaStore
from dragon_voice.media.url_signer import MediaUrlSigner

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
    def __init__(
        self,
        media_store: MediaStore,
        url_signer: Optional[MediaUrlSigner] = None,
    ) -> None:
        self._store = media_store
        # Wave 14 W14-H04: optional HMAC signer.  When provided, every
        # /api/media/{id} GET must carry matching ?exp=&sig= query
        # parameters signed with the server api_token. When None, the
        # handler still works (dev/bootstrap mode).
        self._signer = url_signer

    def register(self, app: web.Application) -> None:
        app.router.add_get("/api/media/{media_id}", self.serve_media)
        app.router.add_post("/api/media/upload", self.upload_media)

    # ── Handlers ────────────────────────────────────────────────────────

    async def serve_media(self, request: web.Request) -> web.Response:
        """GET /api/media/{media_id}?exp=<unix>&sig=<hex> — stream a stored file.

        Wave 14 W14-H04: enforce HMAC signature when the signer is
        configured.  Unsigned requests are rejected with 403.
        """
        media_id = request.match_info["media_id"]

        if self._signer is not None and self._signer.enabled:
            exp = request.query.get("exp")
            sig = request.query.get("sig")
            if not self._signer.verify(media_id, exp, sig):
                logger.info(
                    "serve_media: rejected unsigned/invalid request for %s from %s",
                    media_id, request.remote)
                return web.Response(text="Forbidden", status=403)

        path = await self._store.get_path(media_id)
        if path is None:
            return web.Response(text="Not found", status=404)

        # Derive content-type from extension
        ext = media_id.rsplit(".", 1)[-1].lower() if "." in media_id else ""
        content_type = _CONTENT_TYPES.get(ext, "application/octet-stream")

        # Wave 14 W14-H08 / W14-M05: the blocking open+fh.read() was
        # stalling the event loop for up to 10 MB of camera JPEG per
        # GET. Use web.FileResponse which hands the file off to sendfile
        # on supporting kernels (zero copy, zero event-loop blocking).
        return web.FileResponse(
            path=path,
            headers={
                "Content-Type": content_type,
                "Cache-Control": "max-age=3600",
            },
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

        # Wave 15 W15-H02: reject over-sized uploads BEFORE we read the
        # body.  aiohttp's app-level `client_max_size=32 MB` is the only
        # pre-read cap, but our real limit is 10 MB; the old code read
        # up to 32 MB into memory then compared.  Checking the
        # `Content-Length` header up-front lets us fail fast on big
        # garbage before allocating the buffer.  Chunked uploads with
        # no Content-Length still fall through to the post-read check.
        declared = request.content_length
        if declared is not None and declared > _MAX_UPLOAD_BYTES:
            return web.json_response(
                {"error": "payload too large",
                 "max_bytes": _MAX_UPLOAD_BYTES,
                 "declared_bytes": declared},
                status=413,
                headers={"Connection": "close"},
            )

        raw = await request.read()
        if not raw:
            return web.json_response({"error": "empty body"}, status=400)
        if len(raw) > _MAX_UPLOAD_BYTES:
            return web.json_response({"error": "payload too large"}, status=413)

        # Wave 15 W15-C03: wrap `Image.open` in a context manager so the
        # underlying file descriptor is released even when downstream
        # processing raises.  Before this, every upload leaked one FD
        # into the PIL lazy-load state, eventually crashing with
        # "Too many open files" under sustained load.
        try:
            with Image.open(io.BytesIO(raw)) as src:
                src.load()  # force decode so subsequent ops don't race the FD close
                # W15-H04: .size access on a partially-constructed Image
                # can raise — guard it so we return 400, not 500.
                try:
                    w, h = src.size
                except (AttributeError, OSError, ValueError) as exc:
                    logger.warning("upload_media: size access failed: %s", exc)
                    return web.json_response({"error": "invalid image"}, status=400)

                # Resize so the longest side is at most _JPEG_MAX_PX
                if max(w, h) > _JPEG_MAX_PX:
                    scale = _JPEG_MAX_PX / max(w, h)
                    img = src.resize(
                        (int(w * scale), int(h * scale)), Image.LANCZOS
                    )
                else:
                    img = src.copy()

            # Ensure RGB (BMP frames may be RGBA or palette-mode)
            if img.mode != "RGB":
                img = img.convert("RGB")

            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=_JPEG_QUALITY)
            img.close()
        except (OSError, ValueError) as exc:
            logger.warning("upload_media: could not decode image: %s", exc)
            return web.json_response({"error": "invalid image"}, status=400)

        jpeg_bytes = buf.getvalue()

        session_id = request.headers.get("X-Session-Id", "")
        media_id = await self._store.store(jpeg_bytes, "jpg", session_id=session_id)
        logger.debug("upload_media: stored %s (%d bytes)", media_id, len(jpeg_bytes))

        return web.json_response({"media_id": media_id})
