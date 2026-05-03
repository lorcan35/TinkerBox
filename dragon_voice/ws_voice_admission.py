"""WS /ws/voice admission gate — auth + connection-limit checks.

Wave 23 SOLID-audit follow-up — eighth sub-extract from the
WS-handler family (round 4 spillover, after the seven sub-extracts
from the text-path + vision-turn handlers).

The first ~55 LOC of `_handle_ws_voice` were pre-handshake
admission control: validate the bearer token, enforce the active-
connection cap, return a structured 401 / 503 if either gate
fails.  Pure-function shape — no side effects on the server
beyond reading the configured token + the active-connection
count — but lived inline alongside the WS lifecycle wiring.

This module is the dedicated home for that gate so the WS
handler in `server.py` starts at the actual handshake
(`web.WebSocketResponse` construction).

## API

```python
rejection = check_ws_voice_admission(
    request,
    *,
    expected_token,
    active_connection_count,
    max_connections,
)
```

Returns `None` when the connection should proceed, or a
ready-to-return `web.Response` (401 or 503) when rejected.
The caller short-circuits on a non-None return.

## Why a function returning Optional[Response]

Keeping the rejection flow as a returned value (rather than
raising) lets the caller apply consistent logging / metrics
around it without try-except scaffolding.  Matches the pattern
used by aiohttp middleware throughout the codebase.

## Auth contract (W14-C04)

  * Configured token (non-empty): client MUST present
    `Authorization: Bearer <token>` matching via
    `hmac.compare_digest`.  Mismatch → 401.
  * Empty token (unprovisioned/dev): allow the upgrade but log
    at WARNING so the operator notices they're running
    unauthenticated.  This matches the first-run-bootstrap
    pattern that lets fresh Tab5 flashes connect before the
    operator sets DRAGON_API_TOKEN.

## Error-frame shape (γ3-Dragon, issue #111)

Both rejection responses carry a JSON body with `code` +
`message` so future ops tooling and dashboard introspection can
distinguish auth_failed / server_full from other 401 / 503
sources without parsing prose.  Tab5 (γ3-Tab5 follow-up) still
uses the raw status code for its stop-retry decision because
`esp_websocket_client` doesn't expose the response body.
"""
from __future__ import annotations

import hmac
import logging
from typing import Optional

from aiohttp import web

logger = logging.getLogger(__name__)


def check_ws_voice_admission(
    request: web.Request,
    *,
    expected_token: str,
    active_connection_count: int,
    max_connections: int,
) -> Optional[web.Response]:
    """Validate an incoming /ws/voice upgrade request.

    Returns
    -------
    Optional[web.Response]
        ``None`` when the upgrade should proceed; a
        ``web.json_response`` (401 or 503) when the request must
        be rejected.  The caller short-circuits the handler by
        returning the response directly.

    Two rejection branches in priority order:

      1. **Auth (W14-C04)** — when ``expected_token`` is non-empty,
         require ``Authorization: Bearer <token>`` matching via
         ``hmac.compare_digest``.  Mismatch → 401 with
         ``code="auth_failed"``.
      2. **Connection cap** — when the current active count is at
         or above ``max_connections``, reject with 503 and
         ``code="server_full"``.

    When ``expected_token`` is blank (unprovisioned/dev), log a
    WARNING and let the upgrade through — matches the first-run
    bootstrap pattern.
    """
    # ── Auth gate (W14-C04) ────────────────────────────────────
    if expected_token:
        auth_header = request.headers.get("Authorization", "")
        supplied = (
            auth_header[7:].strip()
            if auth_header.startswith("Bearer ")
            else ""
        )
        if not supplied or not hmac.compare_digest(supplied, expected_token):
            logger.warning(
                "WS /ws/voice: rejecting unauthenticated upgrade from %s "
                "(header_present=%s)",
                request.remote, bool(auth_header),
            )
            # γ3-Dragon (#111): JSON body with `code` so future ops
            # tooling / dashboard introspection can distinguish auth
            # failure from other 401 sources without parsing prose.
            return web.json_response(
                {
                    "code": "auth_failed",
                    "message": "Invalid Dragon token — check Settings.",
                },
                status=401,
            )
    else:
        logger.warning(
            "WS /ws/voice: server.api_token not configured — allowing "
            "unauthenticated WS. Set DRAGON_API_TOKEN to enforce.",
        )

    # ── Connection-cap gate ───────────────────────────────────
    if active_connection_count >= max_connections:
        logger.warning(
            "Connection limit reached (%d), rejecting", max_connections,
        )
        # γ3-Dragon (#111): same JSON-body treatment as the 401 path.
        return web.json_response(
            {
                "code": "server_full",
                "message": "Dragon is at capacity — try again in a moment.",
            },
            status=503,
        )

    return None
