"""Defensive response-header middleware (W14-M06).

Stamps the standard ``X-Content-Type-Options`` / ``X-Frame-Options`` /
``Referrer-Policy`` / ``Content-Security-Policy`` headers onto every
response.  Amplifies the dashboard XSS sweep (W14-H02) — CSP blocks
payloads that slip through the ``escHtml`` gate — and blocks framing +
MIME sniffing for free.

The CSP allows Google Fonts because the dashboard inlines fonts from
there; all other sources stay same-origin.  If the dashboard ever moves
its stylesheet out-of-line the ``'unsafe-inline'`` source can be
tightened.
"""
from __future__ import annotations

from aiohttp import web

SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'"
    ),
}


async def handle_security_headers(request: web.Request, handler):
    """Run the handler and stamp the defensive headers onto the response.

    WebSocket upgrade responses and streaming responses that don't
    expose ``.headers`` in this phase are skipped silently — setting
    CSP on a WS upgrade confuses some clients.
    """
    response = await handler(request)
    try:
        for k, v in SECURITY_HEADERS.items():
            if k not in response.headers:
                response.headers[k] = v
    except (AttributeError, TypeError):
        # WS / streaming responses without a headers mapping at this phase.
        pass
    return response
