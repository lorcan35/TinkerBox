"""Empty-response guard — synthesize a fallback when the LLM
produced no usable text but tools fired (or, optionally, an
apology when nothing produced anything).

Wave 23 SOLID-audit follow-up — second sub-extract from
`_handle_text_body` (round 4, after rich_media_emit #227).

## Why this matters

Several model classes can produce empty / near-empty user-visible
text after a turn:

  * **FC-trained local models** (xLAM, distil-functiongemma,
    LFM2.5-Nova) emit tool calls then stop — leaving the user-
    visible text empty after the parser strips the markup.
    Without a wrap, Tab5 sees `llm` with empty text and silently
    drops the chat bubble.
  * **TinkerClaw / MiniMax** halts after a failed tool call
    without formulating a user-facing reply.

`tools/response_wrap.py` provides:

  * `looks_like_useful_text(s)` — True iff `s` has more than just
    bracket-noise / residual XML / whitespace.  #75 phase 1b
    refinement of the older "strict empty" check.
  * `synthesize_wrap(tool_calls)` — picks a per-tool template
    ("Got it — magenta.", "Searched: 5 results", etc.) from the
    most-recent tool fire.

This module is the WS-handler-side glue that decides:

  1. Is the response text useful?  Return it as-is.
  2. Did any tools fire?  Synthesize a wrap, send it, return it.
  3. Did the caller pass a `fallback_when_no_tools`?  Send that,
     return it.
  4. Otherwise: return the (probably empty) original — let the
     caller's `llm_done` emit handle the empty case however it
     wants.

## Pre-extract behaviour preserved exactly

  * **TinkerClaw branch** — calls with
    `fallback_when_no_tools="Sorry, I couldn't generate a response..."`
    so the apology fires when no tools fired (legacy W15-H09).
  * **Local ConvEngine branch** — calls with
    `fallback_when_no_tools=None` so empty-with-no-tools just
    falls through (matches pre-extract semantics).

The two branches diverged historically — pinned by tests so a
future "let's unify them" refactor doesn't silently change one
or the other.

## API

```python
response_text = await maybe_synthesize_empty_response_wrap(
    ws,
    response_text=response_text,
    tool_calls=conn_state.get("tool_calls_this_turn") or [],
    safe_send_json=self._safe_send_json,
    log_label="tc",                                       # or "local"
    fallback_when_no_tools="Sorry, ...",                  # or None
)
```

Returns the (possibly new) response text.  The caller assigns it
back so any downstream consumer (TTS synth, receipt emit, log
line) sees the wrap-or-original.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from aiohttp import web

from dragon_voice.tools.response_wrap import looks_like_useful_text, synthesize_wrap

logger = logging.getLogger(__name__)


SafeSendJson = Callable[[web.WebSocketResponse, dict], Awaitable[bool]]


async def maybe_synthesize_empty_response_wrap(
    ws: web.WebSocketResponse,
    *,
    response_text: str,
    tool_calls: list,
    safe_send_json: SafeSendJson,
    log_label: str = "local",
    fallback_when_no_tools: Optional[str] = None,
) -> str:
    """If `response_text` isn't useful (#75 phase 1b heuristic),
    synthesize a wrap from `tool_calls` and emit it as an `llm`
    frame.  Return the new text the caller should treat as the
    LLM response.

    Returns `response_text` unchanged when:
      * `looks_like_useful_text(response_text)` is True (happy path),
      * OR no tools fired AND `fallback_when_no_tools` is None
        (legacy local-path behaviour — let empty pass through).

    Args:
        ws: Open WebSocket to Tab5.
        response_text: The post-stream LLM response text.
        tool_calls: The per-turn `tool_calls_this_turn` tracker
            list (from `conn_state`).  Empty list means no tools
            fired this turn.
        safe_send_json: WS-send callable.
        log_label: Prefix for log lines so post-extract you can
            tell TC vs local-path wraps apart in the journal.
        fallback_when_no_tools: When set, sent as the apology
            text when no tools fired.  When None (local-path
            default), empty-with-no-tools falls through silently.
    """
    if looks_like_useful_text(response_text):
        return response_text

    wrap_text: Optional[str] = None
    if tool_calls:
        wrap_text = synthesize_wrap(tool_calls)
        logger.info(
            "#75 phase 1b: %s text path produced near-empty LLM text "
            "(%r) but %d tool(s) fired — emitting template wrap (%d chars)",
            log_label, response_text[:30], len(tool_calls), len(wrap_text),
        )
    elif fallback_when_no_tools is not None:
        wrap_text = fallback_when_no_tools
        logger.warning(
            "W15-H09: %s text path produced zero tokens — emitting fallback response",
            log_label,
        )

    if wrap_text is None:
        # Nothing to do — local-path semantics let the empty
        # pass through; the caller's llm_done will fire with
        # the empty text and Tab5 will drop the bubble.
        return response_text

    if not ws.closed:
        await safe_send_json(ws, {"type": "llm", "text": wrap_text})
    return wrap_text
