"""Agent log API — recent tool-call activity feed.

Surfaces a cross-session ring buffer of the last N tool invocations so
clients (Tab5 ui_agents, dashboard) can show "what has the agent been
doing" without relying on per-connection WebSocket events that are gone
the moment a client disconnects.

Populated by `record_call()` + `record_result()` from the WS voice
handler's existing `_on_tool_call` / `_on_tool_result` hooks (see
server.py).  Pure additive — no DB schema change, no extra writes.

Each entry tracks both the call (tool name + args) and (when received)
the matching result (preview + execution_ms).  Entries with `status =
"running"` are calls that haven't reported a result yet — typically
either an in-flight tool or one that crashed mid-execution.

Ring size is bounded so server memory stays flat under tool-spam.
Default 64; override via `AGENT_LOG_RING_SIZE` env if a deployment
needs more history.

API:
    GET /api/v1/agent_log?since_id=N&limit=50
        →  {"items": [...], "count": N, "head_id": MAX, "tail_id": MIN}

    items[i] = {
        "id":           int,            # monotonic, never reused
        "ts":           int,            # epoch seconds, server-side
        "tool":         str,            # registered tool name
        "args":         dict,           # the call args (or {})
        "status":       "running" | "done",
        "result":       str | None,     # short preview (≤120 chars)
        "execution_ms": int | None,
    }
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from typing import Any, Optional

from aiohttp import web

logger = logging.getLogger(__name__)

# Ring is process-global on purpose: the WS voice handler creates one
# closure per connection, and we want all of them feeding the same log.
_RING_SIZE = max(8, int(os.environ.get("AGENT_LOG_RING_SIZE", "64")))
_ring: deque[dict[str, Any]] = deque(maxlen=_RING_SIZE)
_lock = threading.Lock()
_next_id = 1
_RESULT_PREVIEW_MAX = 120


def _preview(result: Any) -> Optional[str]:
   """Render the tool result as a short single-line preview."""
   if result is None:
      return None
   if isinstance(result, str):
      s = result
   else:
      try:
         s = json.dumps(result, default=str)
      except Exception:
         s = str(result)
   s = s.replace("\n", " ").strip()
   if len(s) > _RESULT_PREVIEW_MAX:
      s = s[: _RESULT_PREVIEW_MAX - 1] + "…"
   return s


def record_call(
    tool: str,
    args: Optional[dict] = None,
    source: str = "dragon",
) -> int:
   """Append a "running" entry to the ring.  Returns the assigned id.

   `source` distinguishes the call's surface (added W7-A.3,
   2026-05-12):
     * "dragon"  — invoked by Dragon's own ToolRegistry (default;
                   preserves the pre-W7-A.3 behaviour for the
                   ToolRegistry chokepoint in tools/registry.py)
     * "gateway" — invoked by the TinkerClaw agent gateway,
                   surfaced via the W7-A SSE tool_call delta path
                   in dragon_voice/llm/tinkerclaw_llm.py
   Unknown sources are stored as-is so a future surface (e.g. MCP
   skill bridge) can name itself without an API change.
   """
   global _next_id
   src = str(source or "dragon").strip() or "dragon"
   with _lock:
      entry_id = _next_id
      _next_id += 1
      _ring.append(
          {
              "id": entry_id,
              "ts": int(time.time()),
              "tool": str(tool or "unknown"),
              "args": dict(args or {}),
              "source": src,
              "status": "running",
              "result": None,
              "execution_ms": None,
          }
      )
   return entry_id


def record_result(
    tool: str,
    result: Any = None,
    execution_ms: Optional[int] = None,
    source: str = "dragon",
) -> None:
   """Mark the most-recent matching `running` entry done.

   Walks newest-first so same-tool-twice-in-one-turn still maps 1:1 to
   the calls.  Matching considers both `tool` and `source` — a
   gateway-side `web_search` and a Dragon-side `web_search` don't
   collide.  If no matching running entry exists (e.g., the call hook
   never fired because of an early-error path), append a synthetic
   done entry tagged with the same source so the result still surfaces.
   """
   global _next_id
   preview = _preview(result)
   src = str(source or "dragon").strip() or "dragon"
   with _lock:
      for rec in reversed(_ring):
         if (
             rec.get("status") == "running"
             and rec.get("tool") == tool
             and rec.get("source", "dragon") == src
         ):
            rec["status"] = "done"
            rec["result"] = preview
            rec["execution_ms"] = execution_ms
            return
      # No matching running entry — append synthetic.
      entry_id = _next_id
      _next_id += 1
      _ring.append(
          {
              "id": entry_id,
              "ts": int(time.time()),
              "tool": str(tool or "unknown"),
              "args": {},
              "source": src,
              "status": "done",
              "result": preview,
              "execution_ms": execution_ms,
          }
      )


def snapshot(since_id: int = 0, limit: int = 50) -> list[dict[str, Any]]:
   """Return entries newer than `since_id`, capped at `limit`.

   Ordering: newest-first (so the consumer can render top-down).
   """
   with _lock:
      items = [
          dict(rec) for rec in reversed(_ring) if int(rec.get("id", 0)) > since_id
      ]
   return items[:limit]


def head_id() -> int:
   with _lock:
      return max((int(r.get("id", 0)) for r in _ring), default=0)


def tail_id() -> int:
   with _lock:
      return min((int(r.get("id", 0)) for r in _ring), default=0)


def reset_for_tests() -> None:
   """Tests only — empty the ring + reset id counter."""
   global _next_id
   with _lock:
      _ring.clear()
      _next_id = 1


class AgentLogRoutes:
   """GET /api/v1/agent_log — recent tool-call activity feed."""

   def register(self, app: web.Application) -> None:
      app.router.add_get("/api/v1/agent_log", self.get_log)

   async def get_log(self, request: web.Request) -> web.Response:
      try:
         since_id = int(request.query.get("since_id", "0"))
      except ValueError:
         since_id = 0
      try:
         limit = int(request.query.get("limit", "50"))
      except ValueError:
         limit = 50
      limit = max(1, min(limit, _RING_SIZE))

      items = snapshot(since_id=since_id, limit=limit)
      # W7-A.3: pre-bucketed source counts for clients that want to
      # render Dragon vs gateway activity separately without re-
      # walking `items` themselves.  Walks the live ring (not the
      # paginated snapshot) so the totals are stable across calls.
      with _lock:
         all_items = list(_ring)
      sources: dict[str, int] = {}
      for rec in all_items:
         s = str(rec.get("source") or "dragon")
         sources[s] = sources.get(s, 0) + 1
      return web.json_response(
          {
              "items": items,
              "count": len(items),
              "head_id": head_id(),
              "tail_id": tail_id(),
              "ring_size": _RING_SIZE,
              "sources": sources,
          }
      )
