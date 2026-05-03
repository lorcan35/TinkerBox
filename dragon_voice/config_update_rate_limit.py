"""Per-connection rate-limit gate for `config_update` WS frames.

Wave 23 SOLID-audit follow-up — seventeenth sub-extract from
the WS-handler family in server.py (round 4 spillover, after
the sixteen prior extracts #227-#242).

Each `config_update` from Tab5 can trigger heavy backend init
(swap STT/TTS/LLM, deep-copy config, persist to DB, etc.).  A
buggy skill or trigger-happy test harness firing rapid mode
swaps can stack these and starve the WS read loop.

The rate-limit gate enforces a 0.5 s minimum between
`config_update` frames per connection.  When a frame arrives
under that threshold, we emit a structured γ-arch
`config_update_rate_limited` (TRANSIENT/SESSION) so Tab5
(γ2-H8) can render a toast like "Slow down — give the swap a
moment."

Pre-extract this 22-LOC chunk lived inline at the top of
`_handle_config_update`.  Now lives in its own dedicated module.

## Audit C1 (#137) closure pinned

Pre-fix the gate was a silent `logger.debug` + `return` — Tab5's
mode-toggle UI sat on its previous local state and the user
assumed the swap landed.  Now we emit the γ-arch event so Tab5
can actually surface the rejection.

## API

```python
allowed = await check_config_update_rate_limit(
    ws,
    *,
    conn_state,
    ws_id,
    safe_send_json,
    min_interval_s=0.5,
)
```

Returns `True` when the swap should proceed (caller continues),
`False` when rate-limited (caller short-circuits — error
already emitted).

The interval is parameterised with a sensible default (0.5 s)
so a future config knob can wire in different limits per
deployment.

## Why state lives on conn_state

The last-update timestamp is stored on `conn_state["_last_config_update_ts"]`
so it's per-connection (a busy device doesn't throttle a quiet
one) and follows the connection's lifecycle (no cleanup needed
on disconnect — conn_state goes away with it).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from aiohttp import web

from dragon_voice.errors import Scope, Severity, error_event

logger = logging.getLogger(__name__)


SafeSendJson = Callable[[web.WebSocketResponse, dict], Awaitable[bool]]


# Default minimum gap between config_update frames per
# connection.  0.5 s is a balance between letting the user
# rapid-toggle modes during testing and preventing a buggy
# skill from storming the swap pipeline.
_DEFAULT_MIN_INTERVAL_S = 0.5

# conn_state key for the last-update timestamp.  Module-level
# so a future audit can grep for "config_update_ts" and find
# every read/write site.
_LAST_TS_KEY = "_last_config_update_ts"


async def check_config_update_rate_limit(
    ws: web.WebSocketResponse,
    *,
    conn_state: Any,                 # ConnState (or compat dict)
    ws_id: str,
    safe_send_json: SafeSendJson,
    min_interval_s: float = _DEFAULT_MIN_INTERVAL_S,
) -> bool:
    """Enforce the per-connection rate-limit on `config_update`
    frames.

    Returns True when the swap should proceed (also stamps
    `conn_state["_last_config_update_ts"]` with the current
    monotonic time so the next call can compare).

    Returns False when the call arrived under `min_interval_s`
    after the previous one.  Emits a γ-arch
    `config_update_rate_limited` (TRANSIENT/SESSION) so Tab5
    can render a toast — preserves the audit C1 (#137)
    closure.

    The error emit is best-effort: skipped when the WS is
    closed (no point alerting a disconnected client) but
    rate-limit gate still fires so the caller short-circuits.
    """
    now = time.monotonic()
    last = conn_state.get(_LAST_TS_KEY, 0.0)
    if now - last < min_interval_s:
        logger.debug("config_update rate-limited on %s", ws_id)
        if not ws.closed:
            await safe_send_json(ws, error_event(
                code="config_update_rate_limited",
                message="Mode swap rate-limited — try again in a moment.",
                severity=Severity.TRANSIENT,
                scope=Scope.SESSION,
            ))
        return False

    conn_state[_LAST_TS_KEY] = now
    return True
