"""Per-connection pipeline audio + event callbacks.

Wave 23 SOLID-audit follow-up — twenty-second sub-extract from
the WS-handler family in server.py (round 4 spillover, after
the twenty-one prior extracts #227-#247).

Every WS-voice connection needs two callbacks wired into its
VoicePipeline:

  * `on_audio(bytes)` — TTS PCM frames flowing FROM the
    pipeline TO Tab5.  Routes through the safe-send wrapper so
    a transient disconnect mid-stream doesn't raise
    ConnectionResetError up into the pipeline's tight loop.
  * `on_event(dict)` — pipeline state events (api_usage,
    cost-tracking, lifecycle).  Forwards via safe_send_json
    AND (for `api_usage` events specifically) persists to the
    DB events table so cost tracking survives restarts.

Pre-extract these were 23 LOC of closures inline in
`_handle_ws_voice` capturing `ws`, `self` (for safe-send +
db), and `conn_state`.  Now lives as a small class with an
explicit constructor — testable in isolation, and the
captured state is visible at the type level.

## API

```python
callbacks = PipelineCallbacks(
    ws,
    conn_state=conn_state,
    safe_send_bytes=safe_send_bytes,
    safe_send_json=safe_send_json,
    db=db,
)
pipeline = VoicePipeline(
    ...,
    on_audio=callbacks.on_audio,
    on_event=callbacks.on_event,
)
```

The class is intentionally minimal — no caching, no batching,
no internal state.  All state lives on the captured `ws` /
`conn_state` so a config-update hot-swap can replace the
pipeline without rebuilding the callbacks.

## Why a class instead of two free functions

Two reasons:

  1. The caller stores both callbacks on conn_state for
     pipeline re-init (audit A04 memory monitor); a single
     instance with two methods is more ergonomic to pass
     around than two separate function objects.
  2. The audit follow-up "WsDispatcher" pattern (eventually)
     will own one PipelineCallbacks per connection — having it
     pre-shaped as a class makes the future migration a no-op.

## v4·D audit P0 fix preserved

`safe_send_bytes` / `safe_send_json` are passed in (DIP) so
this module doesn't need to know about VoiceServer.  Pre-fix
the audio callback used raw `ws.send_bytes` which would raise
ConnectionResetError up into the pipeline's tight loop, where
it was caught by a generic `except Exception` that ALSO
swallowed real exceptions (silent voice-pipeline failures).

## Why on_event persists ONLY api_usage events

DB writes are slow (~ms each); persisting every pipeline
event would balloon the events table and slow the WS path.
`api_usage` is the only one with cost-tracking value (see the
dashboard's API usage chart) so it's the only one worth the
write.  All other events still emit to the WS for live
observability but stop at the DB layer.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

logger = logging.getLogger(__name__)


SafeSendBytes = Callable[[web.WebSocketResponse, bytes], Awaitable[bool]]
SafeSendJson = Callable[[web.WebSocketResponse, dict], Awaitable[bool]]


class PipelineCallbacks:
    """Bundles the on_audio + on_event callbacks for a single
    WS-voice connection.

    Constructed once per connection in `_handle_ws_voice`;
    `pipeline` and any future re-init use `.on_audio` /
    `.on_event` directly.
    """

    def __init__(
        self,
        ws: web.WebSocketResponse,
        *,
        conn_state: dict,
        safe_send_bytes: SafeSendBytes,
        safe_send_json: SafeSendJson,
        db: Optional[Any],
    ) -> None:
        self._ws = ws
        self._conn_state = conn_state
        self._safe_send_bytes = safe_send_bytes
        self._safe_send_json = safe_send_json
        self._db = db

    async def on_audio(self, audio_bytes: bytes) -> None:
        """TTS PCM frames flowing FROM pipeline TO Tab5.

        v4·D audit P0 fix: routes through safe_send_bytes so a
        transient disconnect mid-stream doesn't raise
        ConnectionResetError up into the pipeline's tight loop.
        """
        if self._ws.closed:
            return
        await self._safe_send_bytes(self._ws, audio_bytes)

    async def on_event(self, event: dict) -> None:
        """Pipeline state events (api_usage, cost-tracking,
        lifecycle).  Forwards via safe_send_json AND (for
        `api_usage` events specifically) persists to the DB
        events table so cost tracking survives restarts.

        Failure isolation: DB write errors logged at DEBUG but
        never re-raised — events are observability, not
        session-correctness.

        W4-C (cross-stack audit 2026-05-11): every pipeline-side
        emit gets a `turn_id` field stamped from `conn_state.turn_id`
        before forwarding.  This is THE single chokepoint for
        Dragon→Tab5 frames originating in the voice pipeline
        (stt / llm / llm_done / tts_start / tts_end / api_usage),
        so a one-line injection wires the full round-trip with
        Tab5's outbound turn_id (W4-A) and Dragon's stored
        turn_id (W4-B).  Skip if a caller already set the field
        explicitly — let downstream code override per-event.
        """
        if "turn_id" not in event:
            event["turn_id"] = self._conn_state.get("turn_id", "-")

        if not self._ws.closed:
            await self._safe_send_json(self._ws, event)

        # Persist API usage events for cost tracking.  Other
        # event types (state changes, debug, etc.) emit to WS
        # but stop at the DB layer — keeps the events table
        # focused on cost analysis.
        if event.get("type") == "api_usage" and self._db:
            try:
                await self._db.add_event(
                    "api_usage",
                    session_id=self._conn_state.get("session_id"),
                    device_id=self._conn_state.get("device_id"),
                    data={k: v for k, v in event.items() if k != "type"},
                )
            except Exception as e:
                logger.debug("Callback error: %s", e)
