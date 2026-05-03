"""Vision-turn handler — Tab5 photo upload → LLM analysis.

Wave 23 SOLID-audit follow-up — seventh sub-extract from the
`_handle_*` family in server.py (round 4, after the six
sub-extracts from `_handle_text_body`).

When Tab5 sends a `user_media` WS frame (camera photo for
multimodal LLM analysis), Dragon needs to:

  1. Resolve the media_id to a disk path via MediaStore.
  2. Check the active LLM advertises Modality.VISION (#183 PR 3
     capability-driven check, replacing the old name-substring
     heuristic that broke for fleet/router setups).
  3. Reset the per-turn tool tracker (#75 Phase 1b — vision
     turns can fire tools too).
  4. Stream the vision turn through ConversationEngine
     (`process_text_stream(input_mode="vision", media_id=…)`)
     with the WS PING keepalive so Tab5's PONG-watch doesn't
     trip on a 30-90 s vision generation.
  5. Apply the #75 Phase 1b empty-reply wrap when the model
     fires a tool (e.g. `note` to save a snapshot caption) and
     stops without text.
  6. Emit `llm_done`.

Pre-extract this whole chain lived inline in
`VoiceServer._handle_user_media` (~108 LOC).  Now lives here so
the WS handler family in `server.py` stays focused on the
dispatch surface.

## API

```python
await handle_vision_turn(
    ws,
    *,
    cmd,
    conn_state,
    conversation,
    media_store,
    ws_keepalive,
    safe_send_json,
) -> None
```

`media_store` is the `MediaStore` instance (used for the
`get_path(media_id)` lookup).  `conversation` falls back to
the server's default ConvEngine when conn_state doesn't carry a
per-connection one.  `ws_keepalive` and `safe_send_json` follow
the same DIP pattern used by the round-4 text-path extracts.

## Default prompt

When Tab5 omits `text` from the `user_media` frame, we default
to "What's in this image?" — pre-extract this was hardcoded
inline; lifted to module-level so a future config knob (per-
device caption prompts) has a single place to wire in.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

from dragon_voice.errors import Scope, Severity, error_event
from dragon_voice.tools.response_wrap import looks_like_useful_text, synthesize_wrap

logger = logging.getLogger(__name__)


SafeSendJson = Callable[[web.WebSocketResponse, dict], Awaitable[bool]]
WsKeepaliveFactory = Callable[..., Any]


# Default caption prompt when Tab5 omits `text` from the user_media
# frame.  Tab5 firmware does send a prompt today, but the inline
# fallback predates that contract.
_DEFAULT_VISION_PROMPT = "What's in this image?"


async def handle_vision_turn(
    ws: web.WebSocketResponse,
    *,
    cmd: dict,
    conn_state: dict,
    conversation: Optional[Any],     # ConversationEngine (Optional)
    media_store: Any,                # MediaStore
    ws_keepalive: WsKeepaliveFactory,
    safe_send_json: SafeSendJson,
) -> None:
    """Handle a Tab5 `user_media` (image-upload) frame.

    Routes the multimodal turn through ConversationEngine so the
    user message persists with the multimodal marker (cross-modal
    continuity — a follow-up text turn can still see the photo).

    Three terminal early returns:
      * Missing media (Tab5 sent a stale media_id) → `media_not_found`.
      * No active LLM at all → `no_llm_available` (FATAL).
      * Active LLM doesn't advertise VISION capability →
        `vision_unsupported` (FATAL).

    Mid-turn LLM exception → `vision_failed` (TRANSIENT).  The raw
    exception text is kept in the server log only; the WS frame
    carries a stable user-friendly message so Tab5's caption
    surface doesn't show "list index out of range".

    Always emits `llm_done` on the success path so Tab5 can leave
    the SPEAKING / GENERATING UI state.
    """
    media_id = cmd.get("media_id", "")
    text = cmd.get("text", _DEFAULT_VISION_PROMPT)
    session_id = conn_state.get("session_id", "")

    image_path = await media_store.get_path(media_id)
    if not image_path:
        if not ws.closed:
            await ws.send_json(error_event(
                code="media_not_found",
                message="Image not found — please retake the photo.",
                severity=Severity.TRANSIENT, scope=Scope.MEDIA,
            ))
        return

    # #183 PR 3: capability-driven vision check.  Replaces the old
    # substring check on the model name.  Works uniformly for all
    # backend types — single-backend setups query the active
    # model's declared caps; router setups query the union of caps
    # available in the current tier.
    from dragon_voice.llm.base import Modality
    conv = conn_state.get("conversation") or conversation
    llm_backend = getattr(conv, "_llm", None) if conv else None
    if not llm_backend:
        if not ws.closed:
            await ws.send_json(error_event(
                code="no_llm_available",
                message="No language model is configured.  Check Settings.",
                severity=Severity.FATAL, scope=Scope.LLM,
            ))
        return
    if Modality.VISION not in llm_backend.capabilities:
        if not ws.closed:
            await ws.send_json(error_event(
                code="vision_unsupported",
                message="Image analysis needs a vision-capable model.",
                severity=Severity.FATAL, scope=Scope.LLM,
            ))
        return

    # #75 phase 1b: reset per-turn tool tracker on this path too.
    conn_state["tool_calls_this_turn"] = []

    # #183 PR 3: route through ConversationEngine.process_text_stream
    # with media_id set — ConvEngine persists the multimodal user
    # message via MessageStore (encoded with the multimodal marker)
    # and on context build hydrates it back to an OpenAI image_url
    # content array.  Tools, memory, and cross-modal continuity
    # all work the same as text turns.
    full_response: list[str] = []
    try:
        async with ws_keepalive(ws, label="vision"):
            async for token in conv.process_text_stream(
                session_id=session_id,
                text=text,
                input_mode="vision",
                media_id=media_id,
            ):
                full_response.append(token)
                if not ws.closed:
                    await ws.send_json({"type": "llm", "text": token})
    except Exception as e:
        logger.error("user_media LLM failed: %s", e)
        if not ws.closed:
            # Phase 3 γ1: was raw `str(e)` — leaked Python exception
            # text (e.g. "list index out of range") into Tab5's voice
            # caption.  Now a stable, user-friendly message keyed by
            # `vision_failed`; cause kept in the server log only.
            await ws.send_json(error_event(
                code="vision_failed",
                message="Image analysis failed — please try again.",
                severity=Severity.TRANSIENT,
                scope=Scope.LLM,
            ))
        return

    # #75 phase 1b: vision path gets the same empty-reply guard as
    # the text paths.  Multimodal models can fire a tool (e.g.
    # `note` to save a snapshot caption) and stop without text.
    if not looks_like_useful_text("".join(full_response)):
        tool_calls = conn_state.get("tool_calls_this_turn") or []
        if tool_calls:
            wrap = synthesize_wrap(tool_calls)
            logger.info(
                "#75 phase 1b: vision path emitted near-empty text with "
                "%d tool fire(s) — sending template wrap (%d chars)",
                len(tool_calls), len(wrap),
            )
            if not ws.closed:
                await ws.send_json({"type": "llm", "text": wrap})
            full_response.append(wrap)

    if not ws.closed:
        await ws.send_json({"type": "llm_done", "llm_ms": 0})

    # ConversationEngine already persisted the assistant response
    # via process_text_stream (look for `add_message(role="assistant"
    # ...)` in conversation.py).  No second persist needed here.

    # `safe_send_json` is accepted on the API for parity with the
    # round-4 text-path extracts and for future extension (e.g. an
    # F5 vision-receipt frame); pre-extract code didn't use it.
    _ = safe_send_json
