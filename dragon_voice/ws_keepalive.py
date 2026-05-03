"""Server-side WS keepalive task for the /ws/voice connection.

Wave 23 SOLID-audit follow-up — ninth sub-extract from the
WS-handler family in server.py (round 4 spillover, after the
eight prior extracts).

The keepalive task pings every 15 s with a JSON `pong` data
frame to prevent ngrok's ~30 s idle drop, and tears the WS
down if 3 consecutive pings fail (send timeout or send error).
Pre-extract this lived as a closure inside `_handle_ws_voice`
that captured `_keepalive_running`, `ws`, and `ws_id` from the
outer scope.

This module is the dedicated home for that task.

## API

```python
stop_event = asyncio.Event()
task = asyncio.create_task(
    run_ws_keepalive(ws, ws_id="ws42", stop_event=stop_event)
)
# ... main loop ...
stop_event.set()    # signal clean stop
task.cancel()       # interrupt the in-flight sleep
```

## Why a JSON `pong` data frame, not a WS-level PING

ngrok counts only data frames as activity for its idle-drop
heuristic.  A WS-level PING (which aiohttp's `heartbeat=…`
config emits) keeps the kernel-side TCP connection warm but
does NOT update ngrok's app-level activity counter — so an
otherwise quiet connection still gets dropped after ~30 s
unless there's a data frame on the wire.  This task is the
only thing standing between a slow LLM thinking window and a
mid-turn flap.

## Failure semantics

Three consecutive send timeouts (5 s each) OR three
consecutive send exceptions → close the WS.  This is the
"event loop blocked + dead peer" detector — when inference
holds the GIL too long, sends start failing, and we'd rather
end the WS cleanly than leak the connection until OOM.

## Dead-detection budget (wider than you'd expect)

Worst-case server-side dead-WS detection ≈ 195 s, set by Tab5's
`pingpong_timeout_sec=180` plus the 15 s ping interval.  This is
deliberately wide to tolerate slow Ollama LLM turns without
tearing the WS during a legit thinking window.  Lowering the
PONG budget below ~120 s reintroduces the LLM-flap class
issue #75 was built to fix.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiohttp import web

logger = logging.getLogger(__name__)


# Default tuning constants.  Lifted to module level so a future
# config knob (per-deployment ngrok vs LAN tuning) has one
# canonical place to wire in.
_DEFAULT_INTERVAL_S = 15.0       # 15 s < ngrok's ~30 s idle threshold
_DEFAULT_SEND_TIMEOUT_S = 5.0    # send-timeout per ping
_DEFAULT_MAX_FAILURES = 3        # close WS after 3 consecutive fails


async def run_ws_keepalive(
    ws: web.WebSocketResponse,
    *,
    ws_id: str,
    stop_event: Optional[asyncio.Event] = None,
    interval_s: float = _DEFAULT_INTERVAL_S,
    send_timeout_s: float = _DEFAULT_SEND_TIMEOUT_S,
    max_consecutive_failures: int = _DEFAULT_MAX_FAILURES,
) -> None:
    """Run the keepalive loop until the WS closes, the stop
    event is set, or the failure threshold trips.

    Parameters
    ----------
    ws:
        The aiohttp WebSocketResponse to ping.
    ws_id:
        Connection identifier for log lines.
    stop_event:
        Optional asyncio.Event the caller sets to signal a
        clean stop.  ``task.cancel()`` is the harder-edged
        alternative.  When omitted, the only stop conditions are
        ``ws.closed`` becoming true or the failure threshold.
    interval_s:
        Seconds between pings.  Defaults to 15 s — must be less
        than ngrok's ~30 s idle threshold and the Tab5 PONG
        watchdog window.
    send_timeout_s:
        Seconds to wait for each ping send to complete.  When
        the event loop is blocked by inference holding the GIL,
        sends can hang indefinitely; this bounds the pathology.
    max_consecutive_failures:
        Number of consecutive ping failures (timeouts OR
        exceptions) before closing the WS.

    Returns when:
      * ``ws.closed`` is True at the start of an iteration, OR
      * ``stop_event`` is set, OR
      * The failure counter hits ``max_consecutive_failures``.

    Always closes the WS on the failure-threshold path; never
    raises.
    """
    fail_count = 0
    while not ws.closed:
        if stop_event is not None and stop_event.is_set():
            return
        await asyncio.sleep(interval_s)
        if ws.closed:
            return
        if stop_event is not None and stop_event.is_set():
            return

        # Send a JSON `pong` data frame — ngrok counts data
        # frames as activity.  send_timeout_s prevents the send
        # from blocking indefinitely when the event loop is
        # delayed by GIL contention from inference (US-DQ05).
        try:
            await asyncio.wait_for(
                ws.send_json({"type": "pong"}),
                timeout=send_timeout_s,
            )
            fail_count = 0
        except asyncio.TimeoutError:
            fail_count += 1
            logger.warning(
                "Keepalive send timed out for %s (%d/%d) — "
                "event loop may be blocked",
                ws_id, fail_count, max_consecutive_failures,
            )
            if fail_count >= max_consecutive_failures:
                logger.warning(
                    "Keepalive: %d consecutive timeouts, closing WS %s",
                    max_consecutive_failures, ws_id,
                )
                try:
                    await ws.close()
                except Exception:
                    pass
                return
            continue
        except Exception:
            fail_count += 1
            logger.warning(
                "Keepalive send failed for %s (%d/%d)",
                ws_id, fail_count, max_consecutive_failures,
            )
            if fail_count >= max_consecutive_failures:
                logger.warning(
                    "Keepalive: %d consecutive send failures, closing WS %s",
                    max_consecutive_failures, ws_id,
                )
                try:
                    await ws.close()
                except Exception:
                    pass
                return
            continue
