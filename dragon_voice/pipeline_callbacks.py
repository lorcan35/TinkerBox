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
        billing_config: Optional[Any] = None,
    ) -> None:
        self._ws = ws
        self._conn_state = conn_state
        self._safe_send_bytes = safe_send_bytes
        self._safe_send_json = safe_send_json
        self._db = db
        # W5-B: server-side daily-cap trigger.  When daily_cap_cents > 0
        # and today's total spend exceeds it, emit a cap_downgrade
        # frame once per UTC day per connection.  `_cap_alerted_day`
        # tracks the ISO day we last fired so we don't spam every
        # turn after the cap is hit.
        self._billing_config = billing_config
        self._cap_alerted_day: Optional[str] = None

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
            # W5-B: check the daily cap *after* persistence so the
            # SELECT inside spend_tracker sees the row we just
            # wrote.  Best-effort — any failure here is logged + swallowed
            # so observability never breaks a turn.
            await self._maybe_emit_cap_downgrade()

    async def _maybe_emit_cap_downgrade(self) -> None:
        """W5-B: if BUDGET_DAILY_CENTS is set and today's spend
        exceeds it, emit a one-shot `cap_downgrade` frame so Tab5
        can flip back to LOCAL mode + surface a toast.

        Idempotent across the same UTC day per connection — the
        first hit fires the alert, subsequent turns same-day are
        no-ops.  A WS reconnect resets the per-connection latch
        which is OK: a reconnect after a cap hit means the user
        probably noticed and we can re-alert if cost is still
        accumulating.
        """
        if not self._billing_config:
            return
        cap_cents = getattr(self._billing_config, "daily_cap_cents", 0)
        if cap_cents <= 0:
            return
        if self._db is None:
            return

        # Import inside the method so test fixtures that don't
        # construct a Database aren't forced to satisfy the import
        # graph.
        from dragon_voice.billing.spend_tracker import (
            summarize_spend_for_day,
            today_iso,
        )

        try:
            summary = await summarize_spend_for_day(self._db)
        except Exception as e:
            logger.debug("cap-check spend summary failed: %s", e)
            return

        if summary.total_cents < cap_cents:
            return
        # Once-per-day per connection.
        day = today_iso()
        if self._cap_alerted_day == day:
            return
        self._cap_alerted_day = day

        frame: dict = {
            "type": "cap_downgrade",
            "reason": "daily_cap_hit",
            "spent_cents": summary.total_cents,
            "cap_cents": cap_cents,
            "day": day,
        }
        # Stamp turn_id like every other emit (W4-C).  on_event
        # already does this when frames flow through it; we're
        # bypassing on_event here (would recurse), so stamp manually.
        frame["turn_id"] = self._conn_state.get("turn_id", "-")
        logger.warning(
            "Daily cap hit (spent=%dc, cap=%dc, day=%s) — emitting cap_downgrade to client",
            summary.total_cents, cap_cents, day,
        )
        if not self._ws.closed:
            await self._safe_send_json(self._ws, frame)
