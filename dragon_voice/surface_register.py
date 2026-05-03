"""Surface registration + scheduler offline-queue replay.

Wave 23 SOLID-audit follow-up — fourth sub-handler extract from
`_handle_register` (round 3, after stale_conn_eviction #222,
device_upsert #223, session_handshake #224).

Owns the two post-session-create hooks that wire a fresh
connection into the cross-cutting infrastructure:

  * **Surface registration** — register the connection's WS-send
    callback with the shared SurfaceManager so skills can dispatch
    `widget_*` emissions through it and `widget_action` events
    route back via `handle_action`.

  * **Scheduler offline-queue replay** — drain any notifications
    that were queued while the device was offline and deliver
    them now that a live Tab5Surface exists for this session.

Both are best-effort wiring with explicit failure isolation —
a SurfaceManager hiccup or a scheduler-replay failure must not
block device registration.

## API

```python
await register_surface_and_replay_scheduler(
    ws,
    *,
    surface_mgr,
    scheduler_mgr,
    session_id,
    device_id,
    widget_capabilities,
    safe_send_json,
) -> None
```

No return value — both hooks are fire-and-forget side effects.

## Hook ordering

The replay MUST fire AFTER `SurfaceManager.register_session` so
the scheduler manager can find a live Tab5Surface for this
session when delivering queued frames.  This invariant is
encoded in the function body (the two calls are sequential)
and pinned by a dedicated test.

## DIP

`surface_mgr`, `scheduler_mgr`, and `safe_send_json` are passed
in.  The whole module compiles without importing `VoiceServer`.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

logger = logging.getLogger(__name__)


SafeSendJson = Callable[[web.WebSocketResponse, dict], Awaitable[bool]]


async def register_surface_and_replay_scheduler(
    ws: web.WebSocketResponse,
    *,
    surface_mgr: Optional[Any],        # SurfaceManager
    scheduler_mgr: Optional[Any],      # SchedulerManager
    session_id: str,
    device_id: str,
    widget_capabilities: Optional[dict],
    safe_send_json: SafeSendJson,
) -> None:
    """Wire a fresh connection into Surface + Scheduler.

    Two hooks fire in order:

      1. ``surface_mgr.register_session(session_id, send, caps=...)``
         where ``send`` is a closure routing through
         `safe_send_json` so widget emissions get the same
         transport-close swallow policy as the rest of the WS
         path.  Skipped when `surface_mgr` is None (test paths).

      2. ``scheduler_mgr.replay_queued_for_device(device_id)`` —
         delivers any notifications queued while the device was
         offline.  Failures swallowed with WARNING; the user's
         reminders just stay queued for the next register.
         Skipped when `scheduler_mgr` is None.

    Hook order is important: replay happens AFTER register so the
    scheduler can find a live Tab5Surface for this session when
    delivering the queued frames.
    """
    if surface_mgr is not None:
        async def _surface_send(msg: dict) -> None:
            # v4·D audit P1: route surface sends through
            # _safe_send_json so a transient close mid-widget-emit
            # doesn't bubble into the WS handler and tear the
            # session down.
            if not ws.closed:
                await safe_send_json(ws, msg)
        await surface_mgr.register_session(
            session_id, _surface_send, caps=widget_capabilities,
        )

    # Phase 5 ε2 (issue #131): replay any queued offline notifications
    # for this device.  Best-effort: a queue-drain failure logs a
    # warning but doesn't block registration.
    if scheduler_mgr is not None:
        try:
            replayed = await scheduler_mgr.replay_queued_for_device(device_id)
            if replayed > 0:
                logger.info(
                    "Scheduler offline-queue replay: delivered %d "
                    "frame(s) to %s on session %s",
                    replayed, device_id, session_id,
                )
        except Exception as e:
            logger.warning(
                "Scheduler offline-queue replay failed for %s: %s",
                device_id, e,
            )
