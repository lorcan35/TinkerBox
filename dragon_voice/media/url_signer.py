"""HMAC-signed media URLs for /api/media/{id} access control.

Wave 14 W14-H04: prior to this wave, /api/media/ was in the public
allowlist ("authenticated by ID obscurity") and the id was a 48-bit
uuid4 prefix. Combined with W14-C04 (WS register was also public), any
attacker who registered a session could scrape every media_id emitted
on the WS stream and read the bytes unauthenticated. The same surface
doubled as an SSRF-exfil endpoint for content W14-H03 just closed.

The fix: every ``media`` / ``card`` / ``audio_clip`` event now carries a
URL like ``/api/media/{id}?exp=<unix>&sig=<hex>``. The handler checks
``exp`` hasn't passed and ``sig`` equals HMAC-SHA256(secret, id + exp).
An un-signed URL is rejected when a signing secret is configured.

The secret defaults to ``server.api_token`` (DRAGON_API_TOKEN) so
operators don't need to provision yet another key.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

# Default link lifetime: long enough for Tab5 to download at ngrok-worst-case
# latency + a buffer for slow re-renders; short enough that a stolen URL
# from a WS log scrape stops working by the time an attacker sees it.
_DEFAULT_TTL_SEC = 15 * 60  # 15 minutes


class MediaUrlSigner:
    """Produce and verify HMAC-signed media URLs.

    Construct with the server api_token as the shared secret. Call
    ``sign(media_id)`` when emitting a media event; call ``verify(id,
    exp, sig)`` inside the /api/media handler.

    A blank secret disables signing entirely — the signer becomes a
    no-op that both returns unsigned URLs AND accepts unsigned requests.
    This matches the wave-13 C2 fail-open-during-bootstrap posture.
    """

    def __init__(self, secret: str, ttl_sec: int = _DEFAULT_TTL_SEC) -> None:
        self._secret = (secret or "").strip().encode()
        self._ttl_sec = ttl_sec

    @property
    def enabled(self) -> bool:
        return bool(self._secret)

    # ── public API ──────────────────────────────────────────────────────────

    def sign(self, media_id: str) -> str:
        """Return the path+query that should land in the media event's url.

        With a real secret, returns ``/api/media/{id}?exp=<unix>&sig=<hex>``.
        With no secret, returns ``/api/media/{id}`` unchanged so
        unprovisioned dev boxes keep working.
        """
        safe_id = quote(media_id, safe="._-")
        if not self.enabled:
            return f"/api/media/{safe_id}"
        exp = int(time.time()) + self._ttl_sec
        sig = self._compute_sig(media_id, exp)
        return f"/api/media/{safe_id}?exp={exp}&sig={sig}"

    def verify(
        self, media_id: str, exp: Optional[str], sig: Optional[str]
    ) -> bool:
        """Return True if *media_id* with (*exp*, *sig*) is a valid signed URL.

        When the signer is disabled, returns True unconditionally so
        unprovisioned dev boxes keep functioning. When it's enabled, a
        missing/expired/mismatched sig returns False.
        """
        if not self.enabled:
            return True
        if not exp or not sig:
            return False
        try:
            exp_i = int(exp)
        except (TypeError, ValueError):
            return False
        if exp_i < int(time.time()):
            return False
        expected = self._compute_sig(media_id, exp_i)
        # Constant-time compare to block timing side-channels.
        return hmac.compare_digest(expected, sig)

    # ── internal ────────────────────────────────────────────────────────────

    def _compute_sig(self, media_id: str, exp: int) -> str:
        """HMAC-SHA256 over `<id>|<exp>` hex-encoded, first 32 chars."""
        payload = f"{media_id}|{exp}".encode()
        mac = hmac.new(self._secret, payload, hashlib.sha256).hexdigest()
        # 32 hex = 128 bits of signature strength; enough for a 15-min
        # link while keeping the URL short.
        return mac[:32]
