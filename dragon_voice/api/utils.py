"""Shared API utilities: error responses, pagination, JSON parsing."""

from aiohttp import web


def json_error(message: str, status: int = 400) -> web.Response:
    """Return a JSON error response."""
    return web.json_response({"error": message}, status=status)


def paginated_response(items: list[dict], limit: int, offset: int) -> web.Response:
    """Return a paginated JSON response."""
    return web.json_response({
        "items": items,
        "count": len(items),
        "limit": limit,
        "offset": offset,
    })


def parse_pagination(request: web.Request, default_limit: int = 50,
                     max_limit: int = 200) -> tuple[int, int]:
    """Extract limit/offset from query params with clamping.

    Wave 14 W14-L04: previously a non-numeric ``?limit=abc`` raised
    ``ValueError`` → 500 with a full traceback logged (useful to an
    attacker trying to fingerprint the stack).  Now we coerce with a
    fallback and clamp negative values so pagination stays sane.
    """
    def _int(name: str, default: int) -> int:
        raw = request.query.get(name, str(default))
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default
    limit = max(1, min(_int("limit", default_limit), max_limit))
    offset = max(0, _int("offset", 0))
    return limit, offset


async def parse_json_body(request: web.Request) -> tuple[dict | None, web.Response | None]:
    """Parse JSON body. Returns (body, None) on success, (None, error_response) on failure."""
    try:
        body = await request.json()
        return body, None
    except Exception:
        return None, json_error("Invalid JSON body")
