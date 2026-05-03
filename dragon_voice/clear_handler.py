"""Tab5 `clear` WS command handler.

Wave 23 SOLID-audit follow-up — twelfth sub-extract from the
WS-handler family in server.py (round 4 spillover, after the
eleven prior extracts #227-#237).

When Tab5 sends a `clear` frame (user hit "clear chat"), the
server has to:

  1. Wipe the in-memory pipeline conversation history.
  2. End the current DB session and create a fresh one (so the
     next LLM call gets an empty context — no leaked history
     from the prior chat).
  3. Update `conn_state["session_id"]` to the new session.
  4. Send a `session_start` frame so Tab5 can switch its chat
     view to the new session.

Pre-extract this 33-LOC handler lived inline in
`_handle_ws_voice`'s cmd_type dispatch.  Now lives here so the
dispatch table stays a flat one-liner per command.

## API

```python
await handle_clear_command(
    ws,
    *,
    ws_id,
    conn_state,
    session_mgr,
) -> None
```

`session_mgr` is the SessionManager singleton.  When it (or the
old session id) is missing, the in-memory clear still runs but
the DB-session swap is skipped — preserves the pre-extract
behaviour for boot races and test paths.

## #56 closure (preserved verbatim)

`session_mgr.create_session()` returns a single dict — NOT a
`(dict, bool)` tuple.  That's `get_or_create_session`.  The
old tuple-unpack raised `ValueError` and tore down the WS
handler, leaving Tab5 in RECONNECTING with a blank chat view.
The single-dict shape is preserved here and pinned by the
happy-path test.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from aiohttp import web

logger = logging.getLogger(__name__)


async def handle_clear_command(
    ws: web.WebSocketResponse,
    *,
    ws_id: str,
    conn_state: dict,
    session_mgr: Optional[Any],   # SessionManager (Optional in test paths)
) -> None:
    """Handle a Tab5 `clear` WS frame.

    In-memory clear always runs (when a pipeline is attached).
    DB-session swap runs only when both `session_mgr` and the
    current `session_id` are available — preserves the pre-
    extract behaviour for boot races and test paths.

    Side effects:
      * `pipeline.clear_history()` invoked when attached.
      * `session_mgr.end_session(old_sid)` ends the old session.
      * `session_mgr.create_session(device_id, session_type)`
        creates a fresh one; the new id is written back to
        `conn_state["session_id"]`.
      * `session_start` WS frame emitted with the new session id.

    The `session_start` frame carries `resumed=False` and
    `message_count=0` so Tab5's chat view starts empty.
    """
    pipeline = conn_state.get("pipeline")
    if pipeline:
        pipeline.clear_history()

    # End current session and create a fresh one (clears DB context).
    old_sid = conn_state.get("session_id")
    device_id = conn_state.get("device_id")
    if old_sid and session_mgr:
        await session_mgr.end_session(old_sid)
        # closes #56: create_session returns a single dict,
        # NOT a (dict, bool) tuple — that's
        # get_or_create_session.  The old tuple-unpack raised
        # ValueError and tore down the WS handler, leaving Tab5
        # in RECONNECTING with a blank chat view.  Symptom:
        # every /chat returned 'voice not connected' until
        # next reconnect.
        session = await session_mgr.create_session(
            device_id=device_id, session_type="conversation",
        )
        conn_state["session_id"] = session["id"]
        logger.info(
            "Connection %s: history cleared, new session %s",
            ws_id, session["id"],
        )
        if not ws.closed:
            await ws.send_json({
                "type": "session_start",
                "session_id": session["id"],
                "device_id": device_id,
                "resumed": False,
                "message_count": 0,
            })
    else:
        logger.info(
            "Connection %s: conversation history cleared", ws_id,
        )
