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

import logging
from collections import defaultdict
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)


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


class AgentSkillsRoutes:
    """`GET /api/v1/agent_skills` — agent-skill catalog for mode-3 Tab5."""

    def register(self, app: web.Application) -> None:
        app.router.add_get("/api/v1/agent_skills", self.get_skills)

    async def get_skills(self, request: web.Request) -> web.Response:
        try:
            catalog = build_catalog()
        except Exception:
            logger.exception("agent_skills catalog build failed")
            return web.json_response(
                {"items": [], "count": 0, "static_count": 0, "observed_count": 0,
                 "error": "catalog_unavailable"},
                status=500,
            )
        return web.json_response(catalog)
