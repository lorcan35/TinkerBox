"""Tab5 WS disconnect cleanup chain.

Wave 23 SOLID-audit follow-up — eighteenth sub-extract from
the WS-handler family in server.py (round 4 spillover, after
the seventeen prior extracts #227-#243).

When a Tab5 WebSocket disconnects (clean close, transport drop,
or evicted by P13 stale-conn-eviction), the server has to walk
a chain of cleanups so nothing leaks past the connection's
lifetime:

  1. **Cancel bg_tasks** (W14-C06): per-connection background
     tasks like cap_downgrade speak_system that hold Piper
     subprocess + TTS lock past WS close.
  2. **Cancel handler_tasks** (Phase 1 / #91): in-flight per-
     command tasks (text/media/config) spawned by
     `_spawn_handler_task`.  Without this, a slow text turn
     mid-LLM-stream would keep generating + writing to the
     dead session's DB until naturally complete.
  3. **Unregister surface** (Phase 4g): drop the session's
     SurfaceManager registration so skills with stale refs see
     dropped sends explicitly.
  4. **Pause session** (NOT end — sessions can be resumed on
     reconnect via `requested_session_id` in register).
  5. **Mark device offline** ONLY if no other active connection
     exists for the same device_id.  Multi-tab / multi-device
     scenarios where one WS drops but another stays up MUST
     keep the device marked online.
  6. **Pipeline shutdown** — release Moonshine/Piper/etc.

Pre-extract this 65-LOC chain lived inline as
`VoiceServer._handle_disconnect`.  Now lives in its own
dedicated module with extensive tests.

## API

```python
await handle_disconnect(
    conn_state,
    *,
    active_connections,
    session_mgr,
    db,
    surface_mgr,
)
```

All deps passed in via kwargs (DIP).  Failures along the chain
are isolated: bg_task/handler_task cancellation never raises;
DB ops are wrapped in try/except (DB may be closed during
server shutdown); surface unregister is best-effort; pipeline
shutdown is whatever the pipeline's own `shutdown()` does.

## Why no return value

Disconnect handlers can't usefully fail back to the caller —
the WS is already closing.  All recovery has to happen inside
this function or be logged for ops triage.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


async def handle_disconnect(
    conn_state: dict,
    *,
    active_connections: dict,
    session_mgr: Optional[Any],      # SessionManager
    db: Optional[Any],               # async SQLite layer
    surface_mgr: Optional[Any],      # SurfaceManager
) -> None:
    """Walk the disconnect cleanup chain.

    Steps in order (each is independently failure-isolated):
      1. Cancel + await bg_tasks (W14-C06)
      2. Cancel + await handler_tasks (Phase 1 / #91)
      3. Unregister session surface (Phase 4g)
      4. Pause session via session_mgr (resumable on reconnect)
      5. Mark device offline IFF no other active conn for the
         same device_id (multi-tab safety)
      6. Pipeline.shutdown()

    The session is paused (not ended) so a Tab5 reconnect
    within the resume window can pick up where it left off.
    """
    session_id = conn_state.get("session_id")
    device_id = conn_state.get("device_id")
    ws_id = conn_state.get("ws_id")
    pipeline = conn_state.get("pipeline")

    # ── 1. bg_tasks (W14-C06) ────────────────────────────────
    # Per-connection background tasks (e.g. cap_downgrade
    # speak_system).  Without cancel they hold Piper subproc +
    # TTS lock past WS close.
    bg_tasks = conn_state.get("bg_tasks") or set()
    if bg_tasks:
        for t in list(bg_tasks):
            t.cancel()
        await asyncio.gather(*bg_tasks, return_exceptions=True)

    # ── 2. handler_tasks (Phase 1 / #91) ─────────────────────
    # In-flight per-command tasks (text/media/config) spawned
    # by `_spawn_handler_task`.  Without this, a slow text turn
    # streaming to the LLM when Tab5 disconnects would keep
    # generating tokens + writing assistant messages to the
    # now-dead session's DB until naturally complete.
    handler_tasks = conn_state.get("handler_tasks") or {}
    live = [t for t in handler_tasks.values() if t and not t.done()]
    if live:
        for t in live:
            t.cancel()
        await asyncio.gather(*live, return_exceptions=True)

    # ── 3. Unregister surface (Phase 4g) ─────────────────────
    # Drop the session's surface so skills that kept a reference
    # to it start seeing dropped sends explicitly rather than
    # silently writing to a closed WS.
    if session_id and surface_mgr is not None:
        try:
            await surface_mgr.unregister_session(session_id)
        except Exception:
            logger.debug("surface unregister failed")

    # ── 4 + 5. Session pause + device offline ────────────────
    try:
        if session_id and session_mgr:
            # Pause not end — Tab5 reconnect with the same
            # session_id can resume.
            await session_mgr.pause_session(session_id)

        if device_id and db:
            # Multi-tab safety: only mark offline when no other
            # active connection holds the same device_id.
            other_active = any(
                c.get("device_id") == device_id and c.get("registered")
                for cid, c in active_connections.items()
                if cid != ws_id
            )
            if not other_active:
                await db.set_device_online(device_id, False)
                await db.add_event(
                    "device.disconnected", device_id=device_id,
                    data={"session_id": session_id},
                )
    except (RuntimeError, Exception) as e:
        # DB may be closed during server shutdown — safe to
        # ignore (the disconnect cleanup runs as the server
        # winds down too).
        logger.debug(
            "handle_disconnect db access failed (shutdown?): %s", e,
        )

    # ── 6. Pipeline shutdown ─────────────────────────────────
    if pipeline:
        await pipeline.shutdown()
