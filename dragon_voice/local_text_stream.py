"""Local-path text-stream emitter with mid-stream tool-marker stripping.

Wave 23 SOLID-audit follow-up — sixth sub-extract from
`_handle_text_body` (round 4, after rich_media_emit #227,
empty_response_wrap #228, text_path_receipt #229,
text_path_tts #230, tinkerclaw_text_path #231).

The local-path streaming loop (~50 LOC pre-extract) runs the
ConversationEngine token stream behind a rolling-buffer filter
that prevents stray `<tool>…</args>` markup from leaking into
the chat bubble — a class of bug Wave 10 audit #78 fixed for
small-model output (qwen3:1.7b emits the closing `</tool>` a
token or two after the opening tag).

This module is the dedicated home for that filter + the WS
PING-during-inference keepalive that wraps it.

## API

```python
full_response, response_text = await stream_local_text_with_tool_filter(
    ws,
    *,
    conversation,
    session_id,
    content,
    conn_state,
    ws_keepalive,
)
```

`conversation.process_text_stream` drives the token yield;
`ws_keepalive` is the bound method
`VoiceServer._ws_keepalive_during_inference` passed in as a
factory so this module doesn't need to import or construct it.

## What the filter does

  1. Append every yielded token to `full_response` (untouched —
     the caller still gets the full raw stream for Phase-3
     receipts / rich-media analysis).
  2. Append the same token to a `pending` rolling buffer.
  3. Strip any **complete** `<tool>…</args>` block from the
     buffer (tolerates an extra trailing `>` and case-insensitive
     tag names).
  4. Look for a **partial** opening marker (`<tool`, `</tool`,
     `<args`, `</args`) at the buffer tail.  If one is sitting
     there, hold it back — flushing `<tool>dat` would make Tab5
     show markup that we'd need to retract.
  5. Emit the safe prefix as a `{"type": "llm", "text": …}` frame
     (skipped when ws is closed).
  6. After the stream ends, run one more strip on the residual
     buffer and emit if non-empty.

The returned `response_text` is the joined `full_response` with
the same markup-strip applied — used downstream by the empty-
response wrap, rich-media emit, and TTS synth.

## Why a list[str] for the partial-marker scan

`_PARTIAL_MARKERS` is intentionally ordered by **length descending**
within each opener family so the longest match wins (`</args`
beats `<args` beats `<arg…` partials).  This keeps the held-back
slice as small as possible — important for streaming latency.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable, Tuple

from aiohttp import web

logger = logging.getLogger(__name__)


# `<tool>NAME</tool><args>{json}</args>` — Wave 10 audit #78 closure.
# Tolerates an extra trailing `>` after `</args>` (qwen3:1.7b
# occasionally double-closes), case-insensitive on the tag names.
_TOOL_RE = re.compile(
    r"<tool>[\s\S]*?</tool>\s*<args>[\s\S]*?</args>\s*>?",
    re.IGNORECASE,
)

# Partial opener prefixes scanned at the buffer tail to decide
# how much to hold back.  Longest match wins (rfind returns the
# rightmost occurrence; we keep the maximum index).
_PARTIAL_MARKERS = ("<tool>", "<tool", "</tool", "<args", "</args")


WsKeepaliveFactory = Callable[..., Any]


async def stream_local_text_with_tool_filter(
    ws: web.WebSocketResponse,
    *,
    conversation: Any,               # ConversationEngine
    session_id: str,
    content: str,
    conn_state: dict,
    ws_keepalive: WsKeepaliveFactory,
) -> Tuple[list[str], str]:
    """Stream the local LLM response and emit clean `llm` frames.

    Returns
    -------
    (full_response, response_text):
        * `full_response` is the unfiltered list of tokens as
          yielded by ConversationEngine — the caller uses this
          for the rich-media-emit gate and the receipt path.
        * `response_text` is the joined token stream with the
          tool-marker filter applied — what downstream stages
          (empty-response wrap, rich-media, TTS) consume.

    Mid-stream invariant: never flush a token sequence that ends
    inside a partial `<tool>` / `<args>` marker.  The held-back
    tail joins the next yielded token before re-evaluating.

    The whole loop runs inside `ws_keepalive(ws, label="local_text")`
    so Tab5's PONG-watch (~30 s) doesn't trip and trigger the
    P13 eviction race on slow turns (#75 Phase 1a).
    """
    full_response: list[str] = []
    pending = ""

    async with ws_keepalive(ws, label="local_text"):
        async for token in conversation.process_text_stream(
            session_id=session_id,
            text=content,
            input_mode="text",
            on_tool_call=conn_state.get("on_tool_call"),
            on_tool_result=conn_state.get("on_tool_result"),
            on_tool_error=conn_state.get("on_tool_error"),
        ):
            full_response.append(token)
            pending += token

            # Strip any *complete* tool block sitting in the
            # rolling buffer.  Substitute in-place so remaining
            # prose still flushes below.
            stripped = _TOOL_RE.sub("", pending)
            if stripped != pending:
                pending = stripped

            # Hold back the tail if it looks like a *partial*
            # tool marker so we don't flush `<tool>dat` to the
            # client and then have to retract it.
            hold_at = -1
            for marker in _PARTIAL_MARKERS:
                idx = pending.rfind(marker)
                if idx >= 0 and idx > hold_at:
                    hold_at = idx

            if hold_at >= 0:
                flush, pending = pending[:hold_at], pending[hold_at:]
            else:
                flush, pending = pending, ""

            if flush and not ws.closed:
                await ws.send_json({"type": "llm", "text": flush})

        # End-of-stream: flush whatever remains, stripped one
        # more time so a stray complete block landing in the
        # final tick doesn't slip through.
        pending = _TOOL_RE.sub("", pending)
        if pending and not ws.closed:
            await ws.send_json({"type": "llm", "text": pending})

    response_text = _TOOL_RE.sub("", "".join(full_response))
    return full_response, response_text
