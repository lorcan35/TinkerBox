"""CORS middleware (SEC12).

Adds ``Access-Control-Allow-Origin`` headers to responses whose ``Origin``
matches the allowlist, and answers preflight ``OPTIONS`` requests.
Cross-origin requests from unlisted origins are left unanswered at the
CORS layer — the browser blocks the response.
"""
from __future__ import annotations

from typing import Iterable

from aiohttp import web

# Default allowlist, matching the origins the repo's auth/deploy path
# expects: the dashboard (localhost + Tab5's LAN address) and the public
# ngrok tunnel.  Callers may pass their own set if a deployment needs a
# different list.
DEFAULT_ALLOWED_ORIGINS: frozenset[str] = frozenset({
    "http://localhost:3500",
    "http://127.0.0.1:3500",
    "http://192.168.1.90:8080",
    "https://tinkerclaw-dashboard.ngrok.dev",
})


async def handle_cors(
    request: web.Request,
    handler,
    *,
    allowed_origins: Iterable[str] = DEFAULT_ALLOWED_ORIGINS,
):
    """Apply CORS rules to a single request.

    Returns either the wrapped handler's response (with an
    ``Access-Control-Allow-Origin`` header if the origin is allowed) or
    a short-circuit ``web.Response`` for preflight / rejected-origin
    cases.
    """
    origin = request.headers.get("Origin", "")
    allowed = frozenset(allowed_origins)

    # If origin is not in the allowlist, skip CORS headers entirely
    # (browser will block the cross-origin request).
    if origin not in allowed:
        if request.method == "OPTIONS":
            return web.Response(status=403)
        return await handler(request)

    # Preflight OPTIONS for an allowed origin — answer directly.
    if request.method == "OPTIONS":
        return web.Response(headers={
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-Sample-Rate, Accept, Authorization",
            "Access-Control-Max-Age": "3600",
        })

    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = origin
    return response
