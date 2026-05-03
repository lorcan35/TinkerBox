"""Tab5 `widget_action` WS command handler.

Wave 23 SOLID-audit follow-up — twentieth sub-extract from
the WS-handler family in server.py (round 4 spillover, after
the nineteen prior extracts #227-#245).

When Tab5 fires `widget_action` (user tapped a prompt choice,
live action button, or list row), the server forwards the
event to the SurfaceManager so the originating skill can react.

Pre-extract this 20-LOC handler lived inline in
`_handle_ws_voice`'s cmd_type dispatch.  Now lives in its own
dedicated module.

## Audit P0 closure (v4·D Phase 4g)

Pre-fix this branch didn't exist — every interactive widget
tap was silently dropped into the "Unknown command" logger.
Skills with prompt widgets appeared broken: the user tapped
a choice, nothing happened.  This handler closes that gap by
calling `surface_mgr.handle_action(session_id, card_id, event,
payload)` so the skill's registered handler fires.

## API

```python
await handle_widget_action(
    *,
    cmd,
    conn_state,
    surface_mgr,
)
```

The handler is fire-and-forget from the dispatcher's POV —
SurfaceManager owns the actual delivery to the skill, the
handler just bridges the WS frame to the manager.

## Failure isolation

`SurfaceManager.handle_action` failures (skill exception, dead
session, malformed payload) are caught + logged at EXCEPTION
but never re-raised.  A buggy skill must NOT tear down the WS
read loop — every other widget on the same surface still works.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


async def handle_widget_action(
    *,
    cmd: dict,
    conn_state: dict,
    surface_mgr: Optional[Any],   # SurfaceManager (Optional in tests)
) -> None:
    """Handle a Tab5 `widget_action` WS frame.

    Required cmd fields:
      * `card_id` — the surface card the action targets
      * `event`   — the action name (skill-defined)
      * `payload` — optional dict of action-specific data

    No-op when:
      * Session not yet registered (session_id missing)
      * card_id or event missing (malformed frame)
      * surface_mgr unavailable (boot race / test path)

    Failures isolated: SurfaceManager.handle_action exceptions
    logged at EXCEPTION but never re-raised — a buggy skill
    must NOT tear down the WS read loop.
    """
    sid = conn_state.get("session_id")
    cid = cmd.get("card_id")
    ev = cmd.get("event")
    payload = cmd.get("payload") or {}

    logger.info(
        "widget_action: session=%s card=%s event=%s",
        sid, cid, ev,
    )

    if sid and cid and ev and surface_mgr is not None:
        try:
            await surface_mgr.handle_action(sid, cid, ev, payload)
        except Exception:
            logger.exception("widget_action dispatch failed")
