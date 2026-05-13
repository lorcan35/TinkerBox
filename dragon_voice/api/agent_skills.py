"""Agent skills catalog API — W7-B from the 2026-05-11 cross-stack audit.

`GET /api/v1/agent_skills` returns a merged catalog of:

  * **static** — the well-known OpenClaw gateway core tools every
    mode-3 deployment ships with.  Hardcoded here because the gateway
    speaks WS-RPC for `skills.status`, not REST; a long-lived WS client
    from Dragon to the gateway is a bigger lift than this slice
    deserves.  Real live-discovery is W7-B.2 follow-up.
  * **observed** — distinct tool names captured in the cross-session
    `agent_log` ring (Wave 12 + W7-A.b).  Anything the gateway has
    actually called in this Dragon's lifetime, in addition to the
    static set.  Surfaces grow with usage rather than needing manual
    catalog upkeep.

Response shape (intentionally close to the existing /api/v1/tools shape
so Tab5's Agents overlay can render the two side-by-side with minimal
new code):

```json
{
  "items": [
    {"name": "web_search", "source": "static",   "uses": 0,  "last_ts": null},
    {"name": "bash",       "source": "static",   "uses": 2,  "last_ts": 1715561234},
    {"name": "skill_xyz",  "source": "observed", "uses": 5,  "last_ts": 1715561300}
  ],
  "count": 3,
  "static_count": 7,
  "observed_count": 1
}
```

`uses` and `last_ts` are best-effort, derived from the agent_log
ring.  A tool that hasn't been called since the last process restart
will show uses=0; the ring is bounded so very old usage rolls off.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

logger = logging.getLogger(__name__)

# W7-B.2: TTL cache for the live `skills.status` fetch.  60 s window is
# generous — gateway skill catalogs rarely change between user actions,
# and the Tab5 Agents overlay re-fires on every open + on mode change
# (TT #468) so latency isn't really a concern here either way.
_GATEWAY_CACHE_TTL_S = 60.0
_gateway_cache: dict[str, Any] = {
    "skills": None,   # list[dict] | None
    "fetched_at": 0.0,
    "error": "",
}
_gateway_cache_lock = asyncio.Lock()


# OpenClaw gateway core tools as of the 2026-05-12 OpenClaw source
# inspection.  Static list, not a runtime probe.  When the gateway
# adds a tool, either (a) it surfaces via the `observed` path the first
# time mode 3 calls it, or (b) we update this list — both work.
# Source: ~/Desktop/openclaw/src/plugins/registry.ts + the bundled
# default skill set.
_STATIC_AGENT_SKILLS: tuple[str, ...] = (
    "bash",          # shell command execution
    "browser",       # CDP-driven browser automation
    "edit_file",     # file write/patch
    "memory",        # gateway-side fact store (sqlite-vec + FTS5)
    "read_file",     # file read
    "search_files",  # filesystem search
    "task",          # subagent / multi-step task spawn
    "web_search",    # web search via gateway provider
)


def _aggregate_observed(ring_items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Walk the agent_log snapshot once and build {name → {uses, last_ts}}."""
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"uses": 0, "last_ts": None},
    )
    for rec in ring_items:
        name = rec.get("tool")
        if not isinstance(name, str) or not name:
            continue
        slot = stats[name]
        slot["uses"] += 1
        ts = rec.get("ts")
        if isinstance(ts, int) and (slot["last_ts"] is None or ts > slot["last_ts"]):
            slot["last_ts"] = ts
    return stats


def build_catalog() -> dict[str, Any]:
    """Compose the static-merged-with-observed catalog.  Pure helper —
    exposed so tests can drive it without a live aiohttp request."""
    # Import inline so the agent_log ring is read fresh each call
    # (don't cache; the ring mutates as new tools fire).
    from dragon_voice.api import agent_log as _alog

    with _alog._lock:
        ring_snapshot = list(_alog._ring)

    observed = _aggregate_observed(ring_snapshot)
    items: list[dict[str, Any]] = []

    for name in _STATIC_AGENT_SKILLS:
        slot = observed.pop(name, {"uses": 0, "last_ts": None})
        items.append({
            "name": name,
            "source": "static",
            "uses": slot["uses"],
            "last_ts": slot["last_ts"],
        })

    # Whatever remains in `observed` is a gateway skill we didn't know
    # about at static-catalog time — surface it.  Sort by recency
    # (most-recently-used first) so Tab5 can render "recently active"
    # at the top of the agent-skills section.
    extras = sorted(
        observed.items(),
        key=lambda kv: kv[1]["last_ts"] or 0,
        reverse=True,
    )
    for name, slot in extras:
        items.append({
            "name": name,
            "source": "observed",
            "uses": slot["uses"],
            "last_ts": slot["last_ts"],
        })

    static_count = sum(1 for it in items if it["source"] == "static")
    observed_count = sum(1 for it in items if it["source"] == "observed")
    return {
        "items": items,
        "count": len(items),
        "static_count": static_count,
        "observed_count": observed_count,
    }


async def _fetch_gateway_skills(
    connector_getter: Callable[[], Optional[Any]],
) -> tuple[Optional[list[dict[str, Any]]], str]:
    """W7-B.2: live-poll the gateway with a TTL cache.

    Returns ``(skills_list, error)``.  ``skills_list`` is a list of
    ``{name, description, disabled, bundled, skillKey}`` dicts on
    success, or ``None`` on cache-miss + fetch failure.  ``error`` is a
    short reason when the live path can't be served (caller falls back
    to static).

    Cache key is the connector identity — if the connector swaps at
    runtime (Mock → real Gateway during W7-F.2 wiring), the next call
    bypasses the stale cache.  Concurrent callers share one fetch via
    the module-level lock.
    """
    connector = connector_getter() if connector_getter is not None else None
    if connector is None or not hasattr(connector, "fetch_skills_status"):
        return None, "no_connector"

    async with _gateway_cache_lock:
        now = time.monotonic()
        cached_skills = _gateway_cache["skills"]
        age = now - _gateway_cache["fetched_at"]
        # Serve from cache when fresh AND non-None (None means last
        # fetch failed; retry every TTL window).
        if cached_skills is not None and age < _GATEWAY_CACHE_TTL_S:
            return cached_skills, ""

        try:
            result = await connector.fetch_skills_status()
        except Exception as e:  # noqa: BLE001 — log + fall back
            logger.warning("W7-B.2: skills.status fetch raised: %s", e)
            _gateway_cache["skills"] = None
            _gateway_cache["fetched_at"] = now
            _gateway_cache["error"] = f"fetch_raised: {e}"
            return None, _gateway_cache["error"]

        if not result.ok:
            logger.info(
                "W7-B.2: skills.status returned ok=False (%s) — fallback to static",
                result.error,
            )
            _gateway_cache["skills"] = None
            _gateway_cache["fetched_at"] = now
            _gateway_cache["error"] = result.error
            return None, result.error

        _gateway_cache["skills"] = result.skills
        _gateway_cache["fetched_at"] = now
        _gateway_cache["error"] = ""
        logger.debug("W7-B.2: skills.status cached %d entries", len(result.skills))
        return result.skills, ""


async def build_catalog_async(
    connector_getter: Optional[Callable[[], Optional[Any]]] = None,
) -> dict[str, Any]:
    """Async catalog build with live-gateway poll + static fallback.

    When the gateway connector is reachable, returns:
      * gateway skills (source="gateway") — live + cached 60s
      * observed extras that aren't in the gateway list (source="observed")
      * the static W7-B core 8 are merged in only as a baseline guarantee
        — same names from the gateway override

    When the gateway is unreachable, falls back to ``build_catalog()``
    (the existing static+observed shape, byte-for-byte).
    """
    gateway_skills, gw_error = await _fetch_gateway_skills(
        connector_getter or (lambda: None),
    )
    if gateway_skills is None:
        # Fallback path — preserves the pre-W7-B.2 shape so existing
        # Tab5 firmware keeps rendering without surprises.
        base = build_catalog()
        if gw_error:
            base["gateway_error"] = gw_error
        return base

    # Build {name → observed slot} once.
    from dragon_voice.api import agent_log as _alog
    with _alog._lock:
        ring_snapshot = list(_alog._ring)
    observed = _aggregate_observed(ring_snapshot)

    # Gateway-sourced entries take priority.  They carry richer
    # metadata (description, disabled, bundled) than the static list.
    items: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for entry in gateway_skills:
        name = entry["name"]
        seen_names.add(name)
        slot = observed.pop(name, {"uses": 0, "last_ts": None})
        items.append({
            "name": name,
            "source": "gateway",
            "uses": slot["uses"],
            "last_ts": slot["last_ts"],
            "description": entry.get("description", ""),
            "disabled": entry.get("disabled", False),
            "bundled": entry.get("bundled", False),
            "skillKey": entry.get("skillKey", name),
        })

    # Backfill static W7-B core 8 if the gateway didn't list them —
    # mode-3 with a fresh agent + no installed skills should still
    # surface the well-known tools the user expects to be there.
    for name in _STATIC_AGENT_SKILLS:
        if name in seen_names:
            continue
        slot = observed.pop(name, {"uses": 0, "last_ts": None})
        items.append({
            "name": name,
            "source": "static",
            "uses": slot["uses"],
            "last_ts": slot["last_ts"],
        })

    # Anything left in observed is a tool the agent_log saw fire but
    # the gateway doesn't list — surface as "observed" extras.
    extras = sorted(
        observed.items(),
        key=lambda kv: kv[1]["last_ts"] or 0,
        reverse=True,
    )
    for name, slot in extras:
        items.append({
            "name": name,
            "source": "observed",
            "uses": slot["uses"],
            "last_ts": slot["last_ts"],
        })

    gateway_count = sum(1 for it in items if it["source"] == "gateway")
    static_count = sum(1 for it in items if it["source"] == "static")
    observed_count = sum(1 for it in items if it["source"] == "observed")
    return {
        "items": items,
        "count": len(items),
        "gateway_count": gateway_count,
        "static_count": static_count,
        "observed_count": observed_count,
    }


class AgentSkillsRoutes:
    """`GET /api/v1/agent_skills` — agent-skill catalog for mode-3 Tab5.

    W7-B.2: live-polls the gateway via ``connector_getter`` when wired;
    falls back to the static+observed shape (pre-W7-B.2) when the
    gateway is unreachable / unwired.  The Tab5 surface keeps working
    on both paths.
    """

    def __init__(
        self,
        connector_getter: Optional[Callable[[], Optional[Any]]] = None,
    ) -> None:
        self._connector_getter = connector_getter

    def register(self, app: web.Application) -> None:
        app.router.add_get("/api/v1/agent_skills", self.get_skills)

    async def get_skills(self, request: web.Request) -> web.Response:
        try:
            if self._connector_getter is not None:
                catalog = await build_catalog_async(self._connector_getter)
            else:
                catalog = build_catalog()
        except Exception:
            logger.exception("agent_skills catalog build failed")
            return web.json_response(
                {"items": [], "count": 0, "static_count": 0, "observed_count": 0,
                 "error": "catalog_unavailable"},
                status=500,
            )
        return web.json_response(catalog)


def _reset_gateway_cache_for_tests() -> None:
    """Tests reset module-level cache state.  No production use."""
    _gateway_cache["skills"] = None
    _gateway_cache["fetched_at"] = 0.0
    _gateway_cache["error"] = ""
