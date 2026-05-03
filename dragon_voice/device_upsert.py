"""Device DB upsert with hardware_id collision handling.

Wave 23 SOLID-audit follow-up — second sub-handler extract from
`_handle_register` (round 3, after stale_conn_eviction #222).

Owns the device-row upsert into the `devices` table and the
**D2 audit fix** for `hardware_id` UNIQUE-constraint collisions:
when a second `device_id` registers with a `hardware_id` that's
already claimed by another device, send a γ-arch FATAL/DEVICE
`hardware_id_collision` error to the client and signal the
caller to short-circuit out of `_handle_register`.

## Pre-extract behaviour

The collision case used to bubble `sqlite3.IntegrityError` up to
the WS handler and drop the connection with no Tab5 signal.  Tab5
would auto-reconnect into the same eviction loop until someone
manually intervened.

Audit D2 (#137) fixed this by catching the IntegrityError
specifically and emitting a γ-arch error_event.  This module is
the single home for that fix.

## API

`await upsert_device_with_collision_guard(...) -> bool`

Returns ``True`` iff the upsert succeeded.  Returns ``False`` if
a `hardware_id` collision was detected — in which case the
γ-arch error_event has already been sent to the WS and the
caller MUST short-circuit out of `_handle_register`.

Other (non-collision) IntegrityError types still raise — those
are real bugs and the WS handler's outer try/except is the right
place to catch them.

## DIP

`db` and `safe_send_json` are passed in.  The whole module
compiles without importing `VoiceServer`.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

from dragon_voice.errors import Scope, Severity, error_event

logger = logging.getLogger(__name__)


SafeSendJson = Callable[[web.WebSocketResponse, dict], Awaitable[bool]]


async def upsert_device_with_collision_guard(
    ws: web.WebSocketResponse,
    *,
    db: Any,                          # Database
    device_id: str,
    hardware_id: str,
    name: str = "",
    firmware_ver: str = "",
    platform: str = "",
    capabilities: Optional[Any] = None,
    safe_send_json: SafeSendJson,
) -> bool:
    """Upsert the device into the DB.  Returns True on success.

    On `sqlite3.IntegrityError` mentioning `hardware_id`, sends a
    γ-arch FATAL/DEVICE `hardware_id_collision` error to `ws` and
    returns False so the caller can short-circuit out of register.

    Other IntegrityError flavours are re-raised — those are real
    bugs that the WS handler's outer try/except should surface.
    """
    try:
        await db.upsert_device(
            device_id=device_id,
            hardware_id=hardware_id,
            name=name,
            firmware_ver=firmware_ver,
            platform=platform,
            capabilities=capabilities,
        )
    except sqlite3.IntegrityError as e:
        # D2 audit (#137): the `devices` table has a UNIQUE constraint
        # on `hardware_id`.  A second device_id claiming the same
        # hardware_id is a collision the operator needs to resolve;
        # signal it explicitly to the client rather than dropping the
        # WS silently.
        if "hardware_id" in str(e).lower():
            logger.warning(
                "D2 hardware_id collision: device_id=%s wanted hw=%s but "
                "hw is already claimed by another device — rejecting register",
                device_id, hardware_id,
            )
            if not ws.closed:
                await safe_send_json(ws, error_event(
                    code="hardware_id_collision",
                    message="This hardware ID is already registered to another device.",
                    severity=Severity.FATAL,
                    scope=Scope.DEVICE,
                ))
            return False
        # Non-collision IntegrityError: real bug, let the outer
        # handler catch + log it.
        raise
    return True
