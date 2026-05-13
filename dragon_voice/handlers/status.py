"""Status + health HTTP handlers.

* :func:`handle_status` — ``GET /status`` — small HTML page showing
  backend names, uptime, active connections, session count.  Intended
  for a quick human-readable peek; the real operator UI is the
  dashboard at port 3500.
* :func:`handle_health` — ``GET /health`` — JSON liveness probe.
  Public (no bearer required; see the auth middleware allowlist in
  ``dragon_voice/middleware/auth.py``).

Both take ``server`` as a single handle — the values they read
(uptime baseline, backend names, active-connection count, session
count) are cheap attribute reads and live on the same instance.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from aiohttp import web

from dragon_voice.llm.base import LLMBackend
from dragon_voice.stt.base import STTBackend
from dragon_voice.tts.base import TTSBackend

logger = logging.getLogger(__name__)

# W4-B (audit 2026-05-11): per-subsystem probe budget for `/health`.
# Each backend's `health_check(timeout_s)` is capped at this; the
# overall handler also wraps the gather() with `_HEALTH_GATHER_BUDGET_S`
# so a single stuck backend can never stall the response longer than
# that.  Sized so ngrok healthchecks (default 5 s) and systemd
# `TimeoutStartSec` (default 90 s) never trip.
_HEALTH_PROBE_TIMEOUT_S = 2.0
_HEALTH_GATHER_BUDGET_S = 3.0


async def handle_status(request: web.Request, *, server: Any) -> web.Response:
    """``GET /status`` — small HTML status page."""
    uptime = time.time() - server._start_time
    hours = int(uptime // 3600)
    minutes = int((uptime % 3600) // 60)
    seconds = int(uptime % 60)

    html = f"""<!DOCTYPE html>
<html>
<head><title>Dragon Voice Server</title>
<style>
  body {{ font-family: monospace; background: #1a1a2e; color: #e0e0e0; padding: 2em; }}
  h1 {{ color: #ff6b35; }}
  .info {{ background: #16213e; padding: 1em; border-radius: 8px; margin: 1em 0; }}
  .label {{ color: #0f3460; font-weight: bold; }}
  span.val {{ color: #53d769; }}
</style>
</head>
<body>
  <h1>Dragon Voice Server</h1>
  <div class="info">
    <p>STT Backend: <span class="val">{server._stt_name}</span></p>
    <p>TTS Backend: <span class="val">{server._tts_name}</span></p>
    <p>LLM Backend: <span class="val">{server._llm_name}</span></p>
    <p>Uptime: <span class="val">{hours}h {minutes}m {seconds}s</span></p>
    <p>Active Connections: <span class="val">{len(server._active_connections)}</span></p>
    <p>Total Sessions: <span class="val">{server._session_count}</span></p>
  </div>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


def _pick_backend_to_probe(pool: dict, kind: type) -> Any:
   """Return the first instance in `pool.values()` matching `kind`.

   The pool stores STT/TTS/LLM backends side-by-side keyed by
   signature tuple (see `pipeline._stt_sig` etc).  When a Tab5
   connects in mode-2 (cloud) and another in mode-0 (local), each
   subsystem may have multiple instances pooled.  For /health we
   probe whichever one we find first — operators care about "is at
   least one of each healthy."
   """
   for backend in pool.values():
      if isinstance(backend, kind):
         return backend
   return None


async def _probe(backend: Any, fallback_name: str, timeout_s: float) -> dict:
   """Run a single backend's `health_check`, never raise.

   Uses `backend.name` (the live instance's own name) over the
   server's global-config `fallback_name` when a backend is present —
   so /health reflects the *actually pooled* backend, not whichever
   default appeared in `config.yaml` at boot.  After a hot config-
   swap (mode-2 cloud) those two differ.

   The W4-B contract guarantees `health_check` returns `(ok, detail)`
   without raising.  We still defensive-wrap because backends pre-
   dating W4-B inherit the ABC default but third-party / experimental
   ones might not respect the no-raise rule.
   """
   if backend is None:
      return {"name": "(none)", "ok": True, "detail": "no active backend"}
   live_name = getattr(backend, "name", None) or fallback_name
   try:
      ok, detail = await asyncio.wait_for(
          backend.health_check(timeout_s=timeout_s),
          timeout=timeout_s + 0.5,
      )
   except asyncio.TimeoutError:
      return {
          "name": live_name, "ok": False,
          "detail": f"health_check exceeded {timeout_s}s",
      }
   except Exception as e:  # noqa: BLE001 — must never tear down /health
      logger.warning("health_check raised on %s: %s", live_name, e)
      return {"name": live_name, "ok": False,
              "detail": f"{type(e).__name__}: {e}"[:120]}
   # Trim detail so a chatty backend can't blow the JSON budget.
   return {"name": live_name, "ok": bool(ok),
           "detail": str(detail or "")[:120]}


async def handle_health(request: web.Request, *, server: Any) -> web.Response:
    """``GET /health`` — JSON liveness probe.

    Public endpoint; used by ops + ngrok healthcheck + the
    dashboard's status tile.  Returns uptime in seconds, active
    connection count, and per-subsystem state for STT/LLM/TTS.

    W4-B (audit 2026-05-11): probes each backend via the
    `health_check` method added to the STT/TTS/LLM ABCs.  Pre-W4-B
    this endpoint claimed `status: "ok"` unconditionally — ngrok +
    dashboard + Tab5 reachability all saw green while the LLM
    backend was offline.

    The HTTP status code stays 200 regardless of subsystem state —
    a 503 here would trip ngrok / systemd / Tab5 reconnect logic
    even when the server itself is fine and one *optional* backend
    is down.  The truth lives in the `status` field ("ok" vs
    "degraded") and the per-backend `ok` booleans.
    """
    stt = _pick_backend_to_probe(server._backend_pool, STTBackend)
    tts = _pick_backend_to_probe(server._backend_pool, TTSBackend)
    llm = _pick_backend_to_probe(server._backend_pool, LLMBackend)

    # asyncio.gather concurrently — the slowest subsystem dictates
    # response latency, not the sum.  Each probe is internally capped
    # at _HEALTH_PROBE_TIMEOUT_S; the outer wait_for is a belt-and-
    # suspenders cap in case a backend's wait_for somehow returned
    # control to us late.
    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                _probe(stt, server._stt_name, _HEALTH_PROBE_TIMEOUT_S),
                _probe(tts, server._tts_name, _HEALTH_PROBE_TIMEOUT_S),
                _probe(llm, server._llm_name, _HEALTH_PROBE_TIMEOUT_S),
            ),
            timeout=_HEALTH_GATHER_BUDGET_S,
        )
        stt_state, tts_state, llm_state = results
    except asyncio.TimeoutError:
        stt_state = {"name": server._stt_name, "ok": False,
                     "detail": "gather timeout"}
        tts_state = {"name": server._tts_name, "ok": False,
                     "detail": "gather timeout"}
        llm_state = {"name": server._llm_name, "ok": False,
                     "detail": "gather timeout"}

    all_ok = stt_state["ok"] and tts_state["ok"] and llm_state["ok"]
    return web.json_response(
        {
            "status": "ok" if all_ok else "degraded",
            "uptime_seconds": round(time.time() - server._start_time, 1),
            "active_connections": len(server._active_connections),
            "backends": {
                "stt": stt_state,
                "tts": tts_state,
                "llm": llm_state,
            },
        }
    )
