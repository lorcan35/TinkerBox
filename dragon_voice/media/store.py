"""MediaStore — filesystem-backed media file storage for Dragon.

Stores rendered images (and other binary blobs) with UUID-based IDs.
Tab5 downloads them via HTTP GET /api/media/{id}.
"""

import os
import time
import uuid
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_MEDIA_DIR = "/home/radxa/media"
_DEFAULT_MAX_AGE_HOURS = 24
_DEFAULT_MAX_TOTAL_MB = 500


class MediaStore:
    """Filesystem-backed store for binary media files.

    Media IDs have the form ``{12-char uuid hex}.{ext}``.  The store uses
    synchronous file I/O under the hood (fast enough for local disk) but
    exposes an async interface so callers can await it without surprises in
    an aiohttp context.
    """

    def __init__(
        self,
        media_dir: str = _DEFAULT_MEDIA_DIR,
        max_age_hours: float = _DEFAULT_MAX_AGE_HOURS,
        max_total_mb: float = _DEFAULT_MAX_TOTAL_MB,
    ) -> None:
        self._media_dir = Path(media_dir)
        self._max_age_hours = max_age_hours
        self._max_total_mb = max_total_mb

    # ── Public async API ────────────────────────────────────────────────

    async def store(self, data: bytes, ext: str, session_id: str = "") -> str:
        """Save *data* to disk and return a unique media_id.

        Args:
            data:       Raw bytes to persist.
            ext:        File extension WITHOUT leading dot (e.g. ``"png"``).
            session_id: Optional session tag (unused by storage, kept for
                        future partitioning / logging).

        Returns:
            media_id string of the form ``{32hex}.{ext}``.

        Wave 14 W14-H04: widened the id from 12 → 32 hex chars (48 → 128
        bits of entropy).  The "authenticated by ID obscurity" posture
        relied on 48 bits being too many to guess; combined with
        unauthenticated access at ``/api/media/*`` that's not enough
        against a motivated scraper that already has a session token.
        Full uuid4 hex brings us to the conventional 128-bit safety
        margin; HMAC signing on the URL (see server.py) is the real
        access control.
        """
        self._media_dir.mkdir(parents=True, exist_ok=True)

        media_id = f"{uuid.uuid4().hex}.{ext}"
        dest = self._media_dir / media_id

        dest.write_bytes(data)
        logger.debug(
            "MediaStore.store: %s (%d bytes) session=%s",
            media_id,
            len(data),
            session_id or "-",
        )
        return media_id

    async def get_path(self, media_id: str) -> Optional[str]:
        """Return the filesystem path for *media_id*, or ``None`` if absent.

        The input is sanitised with ``os.path.basename`` so path-traversal
        strings like ``../../etc/passwd`` cannot escape the media directory.
        """
        safe_name = os.path.basename(media_id)
        if not safe_name:
            return None

        candidate = self._media_dir / safe_name
        if candidate.exists():
            return str(candidate)
        return None

    async def cleanup(self) -> None:
        """Remove stale and excess files.

        Two passes:
        1. Delete every file whose mtime is older than *max_age_hours*.
        2. If total size still exceeds *max_total_mb*, delete the oldest
           remaining files until the store is within budget.
        """
        if not self._media_dir.exists():
            return

        files = [
            p for p in self._media_dir.iterdir()
            if p.is_file()
        ]

        now = time.time()
        cutoff = now - self._max_age_hours * 3600
        max_bytes = self._max_total_mb * 1024 * 1024

        # Pass 1: age-based removal
        for p in files:
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    logger.debug("MediaStore.cleanup: removed old file %s", p.name)
            except OSError as exc:
                logger.warning("MediaStore.cleanup: could not remove %s: %s", p.name, exc)

        # Pass 2: size cap — sort survivors by mtime ascending (oldest first)
        survivors = [p for p in self._media_dir.iterdir() if p.is_file()]
        survivors.sort(key=lambda p: p.stat().st_mtime)

        total = sum(p.stat().st_size for p in survivors)
        for p in survivors:
            if total <= max_bytes:
                break
            try:
                size = p.stat().st_size
                p.unlink()
                total -= size
                logger.debug(
                    "MediaStore.cleanup: removed oversized file %s (total now %.1f MB)",
                    p.name,
                    total / 1024 / 1024,
                )
            except OSError as exc:
                logger.warning("MediaStore.cleanup: could not remove %s: %s", p.name, exc)
