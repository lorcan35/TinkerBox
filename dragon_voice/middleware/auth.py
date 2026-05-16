"""Bearer-token auth middleware (Wave 13 C2).

Enforces ``Authorization: Bearer <token>`` on every privileged route.
The expected token is passed in by the caller (read from
``config.server.api_token`` / ``DRAGON_API_TOKEN``) so rotation is
a config change, not a code change.

Public paths are allowlisted:
  - ``/health`` — liveness probe for ops
  - ``/ws/voice`` — the upgrade request itself is allowed through so
    ``_handle_ws_voice`` can enforce auth *before* ``ws.prepare()``; do
    NOT remove this or CORS will bounce the upgrade (W14-C04).
  - ``/dashboard`` — proxied to ``localhost:3500`` and separately gated.
  - ``/api/media/`` — media fetches use an HMAC-signed query-string
    signature instead of the bearer (W14-H04) so Tab5 can pull images
    without presenting the token.
"""
from __future__ import annotations

import hmac

from aiohttp import web

PUBLIC_PREFIXES: tuple[str, ...] = (
    "/health",
    "/ws/voice",
    "/dashboard",
    "/api/media/",
    # #179: video-call web client + its static assets — opens on a
    # phone browser without an API token.  Sensitive surfaces stay
    # under /api/v1/* which still gates auth.
    "/call",
    "/static/",
    # #341 Phase 1: OAuth callback target.  Google redirects the
    # user's phone browser here after consent — the browser has no
    # bearer token, but the PKCE `state` parameter is what
    # authenticates the request (Dragon process holds the only copy
    # of the matching code_verifier).
    "/api/v1/oauth/callback",
)


async def handle_auth(
    request: web.Request,
    handler,
    *,
    expected_token: str,
):
    """Apply bearer-token auth to a single request.

    Returns either the wrapped handler's response, or a short-circuit
    ``web.Response`` for the 401 / 503 cases.  Uses a constant-time
    comparison to avoid timing oracles on credential rejection.
    """
    path = request.path or "/"

    # OPTIONS is answered by the CORS middleware upstream.
    if request.method == "OPTIONS":
        return await handler(request)

    # Allowlist the public prefixes.
    if any(path == p or path.startswith(p) for p in PUBLIC_PREFIXES):
        return await handler(request)

    expected = (expected_token or "").strip()
    if not expected:
        # Fail closed: token not configured but a private path was hit.
        # Deployer must set DRAGON_API_TOKEN or api_token in config.yaml.
        return web.json_response(
            {
                "error": "dragon_api_token_not_configured",
                "message": "Set DRAGON_API_TOKEN in env or api_token in config.yaml",
            },
            status=503,
        )

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return web.json_response(
            {
                "error": "missing_bearer_token",
                "message": "Authorization: Bearer <token> required",
            },
            status=401,
        )
    supplied = auth[7:].strip()
    if not hmac.compare_digest(supplied, expected):
        return web.json_response(
            {"error": "invalid_bearer_token", "message": "token rejected"},
            status=401,
        )
    return await handler(request)
