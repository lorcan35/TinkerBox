"""Post-LLM rich media detection + emission for text-path turns.

Wave 23 SOLID-audit follow-up — first sub-handler extract from
`_handle_text_body` (round 4, after round 1+2+3 decomposed
`_handle_config_update` and `_handle_register`).

Pre-extract this helper was duplicated TWICE inside
`_handle_text_body`:

  * Once in the TinkerClaw bypass branch (server.py:1525-1553).
  * Once in the ConversationEngine path (server.py:1646-1681).

Both copies were ~28 LOC of nearly-identical code: scan the
LLM response for renderable code-blocks / tables / image URLs,
emit a `media_rendering: start` progress signal so Tab5 doesn't
perceive the 1-3 s render as a stalled reply (Audit D4, #137),
strip the rendered content from the text, then emit each media
event.

The two copies differ only in log labels and one defensive
check.  This module collapses them into one entry point with a
`log_label` parameter for log-line provenance.

## API

```python
await emit_rich_media_for_text_turn(
    ws,
    *,
    response_text,
    media_pipeline,
    session_id,
    safe_send_json,
    log_label="local",     # or "tc" for the TinkerClaw branch
) -> None
```

No return value — all side effects flow through the WS.

## Behavior

  1. If `media_pipeline` is None or `response_text` is empty → no-op.
  2. If `media_pipeline.has_renderable_content(response_text)` is
     True AND `ws` is open → send `{"type": "media_rendering",
     "stage": "start"}` so Tab5 shows the user a rendering hint.
  3. Run `media_pipeline.process_response(response_text, session_id)`.
     If it raises → log + swallow (UX nice-to-have, not session
     correctness).
  4. If the pipeline returned media events:
     a. Strip the rendered content from the text and send a
        `text_update` BEFORE the media events (Audit D6 — Tab5's
        last-bubble targeting still points at the text bubble
        when the clear arrives).
     b. Emit each media event in order.

## DIP

`media_pipeline` and `safe_send_json` are passed in.  Module
compiles without importing `VoiceServer`.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

logger = logging.getLogger(__name__)


SafeSendJson = Callable[[web.WebSocketResponse, dict], Awaitable[bool]]


async def emit_rich_media_for_text_turn(
    ws: web.WebSocketResponse,
    *,
    response_text: str,
    media_pipeline: Optional[Any],     # MediaPipeline
    session_id: str,
    safe_send_json: SafeSendJson,
    log_label: str = "local",
) -> None:
    """Detect + emit rich media for a completed LLM text response.

    No-op when:
      * `media_pipeline` is None (test paths, embedded usage).
      * `response_text` is empty.

    Failure isolation:
      All exceptions inside the media-pipeline call are caught +
      logged at WARNING.  Rich-media detection is UX nice-to-have;
      a failed render must not block the next chat turn.

    Args:
        ws: Open WebSocket to Tab5.
        response_text: The full LLM response text to scan.
        media_pipeline: MediaPipeline instance (Optional — None is
            no-op for test paths).
        session_id: Session id (for media-cache key namespacing).
        safe_send_json: WS-send callable (passed in for DIP — used
            for the `media_rendering` progress signal which goes
            through the swallow helper).  The actual media event
            emission uses `ws.send_json` direct because failures
            there are caught by the outer try/except.
        log_label: Prefix for log lines so post-extract you can
            still tell TC vs local-path media events apart in the
            journal.  Default `"local"`; pass `"tc"` from the
            TinkerClaw bypass branch.
    """
    if not response_text or media_pipeline is None:
        return

    # Audit D4 (#137): emit progress BEFORE rendering so Tab5
    # doesn't perceive the 1-3 s code-block render as a stalled
    # reply.  Goes through safe_send_json to swallow transport
    # close mid-reply.
    if not ws.closed and media_pipeline.has_renderable_content(response_text):
        await safe_send_json(ws, {
            "type": "media_rendering",
            "stage": "start",
        })

    try:
        media_events = await media_pipeline.process_response(
            response_text, session_id,
        )
        logger.info(
            "MediaPipeline (%s): %d event(s) for response len=%d",
            log_label, len(media_events), len(response_text),
        )
        if media_events:
            # Audit D6: send text_update BEFORE media events.  Tab5's
            # ui_chat_update_last_message targets the *last* chat
            # bubble.  If we send media first, the image becomes
            # "last" and the empty-string text_update would remove
            # the wrong row.  text_update first clears the streamed
            # markdown bubble; media events then append the
            # rendered JPEG below.
            cleaned = media_pipeline.strip_rendered_content(
                response_text, media_events,
            )
            logger.info(
                "strip_rendered_content (%s): %d→%d chars",
                log_label, len(response_text), len(cleaned),
            )
            if not ws.closed:
                await ws.send_json({"type": "text_update", "text": cleaned})
                logger.info(
                    "Sent text_update (D6 %s) with %d chars",
                    log_label, len(cleaned),
                )
        for event in media_events:
            if not ws.closed:
                await ws.send_json(event)
    except Exception as e:
        logger.warning("Media detection (%s) failed: %s", log_label, e)
