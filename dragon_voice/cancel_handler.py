"""Tab5 `cancel` WS command handler.

Wave 23 SOLID-audit follow-up — eleventh sub-extract from the
WS-handler family in server.py (round 4 spillover, after the
ten prior extracts #227-#236).

When Tab5 sends a `cancel` frame mid-turn (user hit STOP), the
server has to:

  1. Cancel any in-flight handler task in the `text`, `media`,
     or `config` slots (Phase 1 / issue #91).
  2. Cancel the voice pipeline (audit A1 / #137 — also kills
     in-flight Piper subproc since text path can call
     `_tts.synthesize` directly outside `_processing`).
  3. Discard any scheduler-fired widgets that deferred during
     this turn (audit B1 / #165 — user cancelled, the deferred
     reminder should also disappear).
  4. Send a `cancel_ack` so Tab5 has positive feedback that the
     cancel landed (Tab5 already transitions to READY locally
     on cancel-send; the ack is the protocol-clean half).

Pre-extract this 60-LOC handler lived inline in
`_handle_ws_voice`'s cmd_type dispatch.  Now lives here so the
dispatch table stays a flat one-liner per command.

## API

```python
await handle_cancel_command(
    ws,
    *,
    ws_id,
    conn_state,
    surface_mgr,
    safe_send_json,
) -> None
```

`surface_mgr` is the SurfaceManager singleton (used for the
deferred-widget discard).  `safe_send_json` follows the same
DIP pattern used by the round-4 text-path extracts.

## What's intentionally NOT in this module

The actual task lifecycle (which task lives in which slot,
when slots get populated, when they get cleared on completion)
is the dispatcher's responsibility — this module just walks
the slots and cancels.  Adding a new slot is a one-line
change here AND in the dispatcher; the audit's WsDispatcher
follow-up will collapse both.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

logger = logging.getLogger(__name__)


SafeSendJson = Callable[[web.WebSocketResponse, dict], Awaitable[bool]]


# Handler-task slots cancelled on a `cancel` frame.  Order is
# the legacy walk order (text → media → config); preserved for
# log-line stability.
_CANCEL_SLOTS = ("text", "media", "config")


async def handle_cancel_command(
    ws: web.WebSocketResponse,
    *,
    ws_id: str,
    conn_state: dict,
    surface_mgr: Optional[Any],      # SurfaceManager (Optional in test paths)
    safe_send_json: SafeSendJson,
) -> None:
    """Handle a Tab5 `cancel` WS frame.

    Walks the `text` / `media` / `config` handler-task slots,
    cancels any in-flight pipeline, discards deferred widgets,
    and sends `cancel_ack` with the per-source breakdown.

    Side effects (per pre-extract behaviour):
      * `conn_state["handler_tasks"][slot]` set to ``None`` for
        any cancelled slot (so the dispatcher knows the slot is
        free again).
      * `pipeline.cancel()` invoked when a pipeline is attached
        (idempotent — safe even when nothing is in flight).
      * `surface_mgr.discard_deferred(session_id)` invoked when
        both surface manager and session id are available.

    The `cancel_ack` payload always carries a `cancelled` list
    even when nothing was cancelled — Tab5 uses an empty list
    as a signal that the cancel arrived but found nothing in
    flight (e.g. user hit STOP after the turn already finished).
    """
    cancelled_what: list[str] = []
    handler_tasks = conn_state.setdefault("handler_tasks", {})
    pipeline = conn_state.get("pipeline")

    # ── Cancel in-flight handler tasks ──────────────────────
    for slot in _CANCEL_SLOTS:
        t = handler_tasks.get(slot)
        if t and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                # Task may have raised mid-cancel; the task
                # itself logged at the relevant level.  Don't
                # propagate to the WS loop or the loop dies.
                pass
            handler_tasks[slot] = None
            cancelled_what.append(slot)

    # ── Cancel the pipeline (audit A1 / #137) ───────────────
    # Always invoked — text path calls pipeline._tts.synthesize
    # directly (see _handle_text), so a Piper subprocess can be
    # alive even when no handler-task slot was occupied or
    # _processing is False.  pipeline.cancel() is idempotent.
    if pipeline:
        logger.info("Connection %s: cancel → pipeline.cancel", ws_id)
        await pipeline.cancel()
        cancelled_what.append("pipeline")

    # ── Discard deferred scheduler widgets (audit B1 / #165) ─
    # User cancelled the turn, so any reminder that fired during
    # it should also disappear (a fresh fire cycle will pop on
    # the next turn-idle window if the scheduler still wants to
    # deliver it).
    sid = conn_state.get("session_id")
    if surface_mgr is not None and sid:
        dropped = surface_mgr.discard_deferred(sid)
        if dropped:
            logger.info(
                "Connection %s: cancel → discarded %d deferred widget(s)",
                ws_id, dropped,
            )
            cancelled_what.append(f"deferred:{dropped}")

    # ── Send cancel_ack ─────────────────────────────────────
    # Tab5 transitions to READY locally on cancel-send and may
    # otherwise see late `llm` tokens that were already in TCP
    # flight.  Tab5-side fix at voice.c:752 covers the late-
    # token case directly; this ack is the protocol-clean half.
    await safe_send_json(ws, {
        "type": "cancel_ack",
        "cancelled": cancelled_what,
    })
