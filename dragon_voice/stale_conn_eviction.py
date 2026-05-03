"""Stale-connection eviction (P13 audit fix).

Wave 23 SOLID-audit follow-up — first sub-handler extract from
`_handle_register` (round 3, after the round-2 series that
decomposed `_handle_config_update` into 8 focused modules).

When a Tab5 device reconnects faster than aiohttp detects the
old TCP close, two `_active_connections` entries can end up with
the same `device_id`.  The new register frame triggers this
eviction routine, which:

  1. Walks `_active_connections` for any other entry with the
     same `device_id` AND `registered=True`.
  2. Sends a γ-arch `device_evicted` error frame to the OLD
     connection's `_on_event` closure (issue #108) so Tab5 can
     distinguish "another device claimed this session" from a
     generic network drop and avoid the auto-reconnect loop.
  3. Shuts down the old pipeline (best-effort — log on failure
     but proceed).
  4. Pauses the old session via `SessionManager.pause_session`
     so it can be resumed by the new connection if needed.
  5. Marks `registered=False` and pops the old entry from
     `_active_connections` so `_handle_disconnect` is a no-op
     when aiohttp eventually fires it.

## Why a separate module

This is **policy** (when to evict, what to send to the old
client, what teardown order to use) — distinct from the rest of
`_handle_register`'s responsibilities (DB upsert, session
create, pipeline init, tool wiring).  The unit test
[`tests/test_device_evicted.py`](tests/test_device_evicted.py)
already drives this slice directly; pulling it into its own
module makes the seam an explicit public function instead of
"inline code in a 522-LOC handler".

## DIP

`active_connections` (the dict to mutate) and `session_mgr` (the
SessionManager to pause via) are passed in rather than reaching
into `VoiceServer`.  The whole module compiles without importing
`VoiceServer`.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from dragon_voice.errors import Scope, Severity, error_event

logger = logging.getLogger(__name__)


async def evict_stale_connections_for_device(
    *,
    active_connections: dict,
    session_mgr: Optional[Any],
    device_id: str,
    new_ws_id: str,
) -> int:
    """Evict any connection in `active_connections` that's
    registered to the same `device_id` as the new connection.

    P13 audit fix.  Race condition: a new Tab5 WS arrives before
    aiohttp detects the old TCP close.  The old keepalive task is
    still running, the old pipeline isn't shut down yet, and we
    end up with two `_active_connections` entries claiming the
    same device.

    Args:
        active_connections: The server's `_active_connections`
            dict — mutated in place (entries removed for evicted
            connections).
        session_mgr: SessionManager for `pause_session` — None in
            test paths skips the pause.
        device_id: The device ID being registered (we evict any
            other connection registered to the same device).
        new_ws_id: The new connection's ws_id — we skip ourselves
            in the loop so this is safe to call BEFORE the new
            connection is added to `active_connections` AND after.

    Returns:
        The number of stale connections evicted (for caller logging
        and tests).
    """
    evicted = 0
    for old_ws_id, old_conn in list(active_connections.items()):
        if old_ws_id == new_ws_id:
            continue  # Skip ourselves
        if old_conn.get("device_id") != device_id:
            continue
        if not old_conn.get("registered"):
            continue

        logger.warning(
            "P13: Device %s already has connection %s — evicting stale connection",
            device_id, old_ws_id,
        )

        # γ2-M5 (issue #108): tell the old client why it's being
        # disconnected BEFORE we tear its pipeline down.  Pre-fix
        # the client just saw TCP close and had no signal that
        # another instance had claimed the slot — Tab5 would then
        # auto-reconnect into the same eviction loop.  FATAL/DEVICE
        # is the "operator action needed; do NOT auto-reconnect"
        # signal Tab5 (γ2-H8) routes to the caption + retry banner.
        old_on_event = old_conn.get("_on_event")
        if old_on_event:
            try:
                await old_on_event(error_event(
                    code="device_evicted",
                    message="Another device claimed this session.",
                    severity=Severity.FATAL,
                    scope=Scope.DEVICE,
                ))
            except Exception as e:
                # Stale / closed WS — eviction must still proceed.
                # The user-visible signal is best-effort; the new
                # connection's success matters more.
                logger.debug(
                    "P13: device_evicted notice not delivered to %s: %s",
                    old_ws_id, e,
                )

        # Shut down the old pipeline (best-effort).
        old_pipeline = old_conn.get("pipeline")
        if old_pipeline:
            try:
                await old_pipeline.shutdown()
            except Exception as e:
                logger.warning(
                    "P13: old pipeline shutdown failed for %s: %s",
                    old_ws_id, e,
                )
            old_conn["pipeline"] = None

        # Pause the old session (not end — it might be resumed by
        # the new connection).
        old_sid = old_conn.get("session_id")
        if old_sid and session_mgr is not None:
            await session_mgr.pause_session(old_sid)

        # Mark as unregistered so _handle_disconnect won't mark
        # device offline.
        old_conn["registered"] = False

        # Remove from active connections — _handle_disconnect will
        # be a no-op.
        active_connections.pop(old_ws_id, None)

        logger.info(
            "P13: Evicted stale connection %s for device %s",
            old_ws_id, device_id,
        )
        evicted += 1

    return evicted
