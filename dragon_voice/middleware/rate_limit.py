"""Per-IP + per-path rate-limit middleware (Wave 15 W15-H01, W15-H06).

Throttles a small set of state-changing or stream-amplifying endpoints
on a fixed window keyed on (client_ip, method, path).  In-memory only
— if the process restarts, counters reset.  That's fine for this threat
model (single-tenant deployment, goal is to stop loop-bugs + obvious
DoS, not to enforce commercial quotas).

The bucket dict is supplied by the caller so state lives on the
:class:`VoiceServer` instance and survives across middleware invocations
without any module-level globals.
"""
from __future__ import annotations

import logging
import time
from typing import Iterable

from aiohttp import web

logger = logging.getLogger(__name__)

# (method, path_prefix, max_requests, window_seconds).  Keep the list
# short; the matcher walks it per request.  Endpoints not listed here
# are unthrottled.
DEFAULT_RATE_LIMIT_RULES: tuple[tuple[str, str, int, int], ...] = (
    ("DELETE", "/api/v1/devices/",          20, 60),
    ("DELETE", "/api/v1/sessions/",         20, 60),
    ("DELETE", "/api/v1/messages",          20, 60),
    ("DELETE", "/api/v1/memory/",           30, 60),
    ("DELETE", "/api/v1/documents/",        10, 60),
    ("DELETE", "/api/v1/config/",           20, 60),
    ("POST",   "/api/v1/sessions/",         30, 60),  # /end, /pause, /resume
    # W15-H06: SSE reconnect amplification — a broken client can re-open
    # the chat stream 30 times/sec on tab-flap.  Cap at 20/min per IP +
    # session to give legit retries room but stop the storm.
    ("POST",   "/api/v1/sessions/",         20, 60),  # covers /chat too
    ("POST",   "/api/media/upload",         30, 60),  # 10 MB upload × spam DoS
)

# Above this count we GC entries whose window started more than
# ``_GC_STALE_SECONDS`` ago.  Each bucket is ~200 B so 4096 ≈ 800 KB —
# worth reclaiming before it drifts.
_GC_TRIGGER_ENTRIES = 4096
_GC_STALE_SECONDS = 600


async def handle_rate_limit(
    request: web.Request,
    handler,
    *,
    buckets: dict,
    rules: Iterable[tuple[str, str, int, int]] = DEFAULT_RATE_LIMIT_RULES,
):
    """Apply rate-limit rules to a single request.

    ``buckets`` is mutated in place — caller should pass the same dict
    across all requests so windows are shared.  Returns either the
    wrapped handler's response or a 429 with ``Retry-After`` set.
    """
    path = request.path
    method = request.method

    # Walk the rule table and keep the tightest (smallest cap) match.
    matched: tuple[int, int] | None = None
    for m, p, cap, win in rules:
        if method == m and path.startswith(p):
            if matched is None or cap < matched[0]:
                matched = (cap, win)
    if matched is None:
        return await handler(request)

    # Peername behind ngrok/proxy is the proxy IP, which is fine for
    # single-tenant threat model.
    peer = request.transport.get_extra_info("peername") if request.transport else None
    client = peer[0] if peer else "unknown"
    cap, win = matched
    key = (client, method, path)

    now = time.monotonic()
    bucket = buckets.get(key)
    if bucket is None or now - bucket["window_start"] >= win:
        buckets[key] = {"window_start": now, "count": 1}
    else:
        bucket["count"] += 1
        if bucket["count"] > cap:
            retry_after = int(win - (now - bucket["window_start"])) + 1
            logger.warning(
                "rate-limit: %s %s from %s (%d/%d in %ds)",
                method, path, client, bucket["count"], cap, win,
            )
            return web.json_response(
                {
                    "error": "rate_limited",
                    "message": f"limit {cap} requests per {win} s for this path",
                    "retry_after_seconds": retry_after,
                },
                status=429,
                headers={"Retry-After": str(retry_after)},
            )

    # Cheap periodic GC so the dict doesn't grow unbounded over long
    # uptimes.  The caller's dict is mutated in place.
    if len(buckets) > _GC_TRIGGER_ENTRIES:
        cutoff = now - _GC_STALE_SECONDS
        stale = [k for k, v in buckets.items() if v["window_start"] <= cutoff]
        for k in stale:
            buckets.pop(k, None)

    return await handler(request)
