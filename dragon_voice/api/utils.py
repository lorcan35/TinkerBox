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
    """Extract limit/offset from query params with clamping."""
    limit = min(int(request.query.get("limit", str(default_limit))), max_limit)
    offset = int(request.query.get("offset", "0"))
    return limit, offset


async def parse_json_body(request: web.Request) -> tuple[dict | None, web.Response | None]:
    """Parse JSON body. Returns (body, None) on success, (None, error_response) on failure."""
    try:
        body = await request.json()
        return body, None
    except Exception:
        return None, json_error("Invalid JSON body")
