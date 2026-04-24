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

import time
from typing import Any

from aiohttp import web


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


async def handle_health(request: web.Request, *, server: Any) -> web.Response:
    """``GET /health`` — JSON liveness probe.

    Public endpoint; used by ops + ngrok healthcheck + the
    dashboard's status tile.  Returns uptime in seconds, active
    connection count, and the three backend names.
    """
    return web.json_response(
        {
            "status": "ok",
            "uptime_seconds": round(time.time() - server._start_time, 1),
            "active_connections": len(server._active_connections),
            "backends": {
                "stt": server._stt_name,
                "tts": server._tts_name,
                "llm": server._llm_name,
            },
        }
    )
