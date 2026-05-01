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


def record_call(tool: str, args: Optional[dict] = None) -> int:
   """Append a "running" entry to the ring.  Returns the assigned id."""
   global _next_id
   with _lock:
      entry_id = _next_id
      _next_id += 1
      _ring.append(
          {
              "id": entry_id,
              "ts": int(time.time()),
              "tool": str(tool or "unknown"),
              "args": dict(args or {}),
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
) -> None:
   """Mark the most-recent matching `running` entry done.

   Walks newest-first so same-tool-twice-in-one-turn still maps 1:1 to
   the calls.  If no running entry matches (e.g., the call hook never
   fired because of an early-error path), append a synthetic done entry
   so the result still surfaces.
   """
   global _next_id
   preview = _preview(result)
   with _lock:
      for rec in reversed(_ring):
         if rec.get("status") == "running" and rec.get("tool") == tool:
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
      return web.json_response(
          {
              "items": items,
              "count": len(items),
              "head_id": head_id(),
              "tail_id": tail_id(),
              "ring_size": _RING_SIZE,
          }
      )
