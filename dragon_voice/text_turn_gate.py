"""Text-turn entry guard + B1 turn-busy bracket.

Wave 23 SOLID-audit follow-up — twenty-first sub-extract from
the WS-handler family in server.py (round 4 spillover, after
the twenty prior extracts #227-#246).

Every text turn coming through `_handle_text` needs three
gates around the actual body invocation:

  1. **Precondition guard**: not-yet-registered or empty-content
     frames must short-circuit before reaching the LLM.
  2. **Per-turn tool tracker reset** (#75 phase 1b): each
     incoming text starts a fresh turn; the empty-reply guard
     downstream synthesises a template wrap from what actually
     fired during this turn.
  3. **B1 turn-busy bracket** (audit B1 / #165): mark the
     surface manager's turn-busy flag so scheduler-fired
     widgets defer until this turn completes — prevents the
     `llm token / widget_card / llm token` interleave that
     made chat unreadable.

Pre-extract this 39-LOC wrapper lived inline as
`VoiceServer._handle_text`.  Now lives in its own dedicated
module with the body invocation passed in as a callable.

## API

```python
await invoke_with_text_turn_gate(
    ws,
    *,
    conn_state,
    cmd,
    conversation,
    surface_mgr,
    safe_send_json,
    body_fn,
)
```

`body_fn` is the actual text-turn body — invoked as
`await body_fn(ws, conn_state, cmd, text, session_id, content)`.
The wrapper owns the gating; `body_fn` owns the LLM/TTS work.

## Precondition guard

  * No `session_id` in conn_state (not yet registered) → emit
    FATAL `session_invalid` error_event, return without invoking
    body.
  * No `conversation` engine wired (boot race / test path) →
    same error event, return.
  * Empty / whitespace-only content → silent return (no error;
    Tab5 may have sent a blank frame as a heartbeat).

## B1 bracket failure isolation

`mark_turn_end` failure is logged at EXCEPTION but never
re-raised — a bug in the surface manager must NOT prevent the
next text turn.  The bracket runs in a `try/finally` so
`mark_turn_end` fires even when `body_fn` raises.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

from dragon_voice.errors import Scope, Severity, error_event

logger = logging.getLogger(__name__)


SafeSendJson = Callable[[web.WebSocketResponse, dict], Awaitable[bool]]
TextBodyFn = Callable[
    [web.WebSocketResponse, dict, dict, str, str, str],
    Awaitable[None],
]


async def invoke_with_text_turn_gate(
    ws: web.WebSocketResponse,
    *,
    conn_state: dict,
    cmd: dict,
    conversation: Optional[Any],         # ConversationEngine
    surface_mgr: Optional[Any],          # SurfaceManager
    body_fn: TextBodyFn,
) -> None:
    """Run the text-turn body inside the precondition guard +
    B1 turn-busy bracket.

    No-op when:
      * Not registered (no `session_id`) — emits `session_invalid`
        FATAL error_event then returns.
      * No conversation engine (boot race) — same error.
      * Empty content (Tab5 heartbeat) — silent return.

    Side effects on success:
      * `conn_state["tool_calls_this_turn"] = []` (per-turn
        reset for the empty-reply wrap downstream).
      * `surface_mgr.mark_turn_start(session_id)` before body.
      * `surface_mgr.mark_turn_end(session_id)` after body
        (guaranteed via try/finally).
      * INFO log of the first 80 chars of the text input.

    `mark_turn_end` failure is logged at EXCEPTION but never
    re-raised — must not block the next text turn.
    """
    session_id = conn_state.get("session_id")
    if not session_id or not conversation:
        await ws.send_json(error_event(
            code="session_invalid",
            message="Not registered — send register first.",
            severity=Severity.FATAL, scope=Scope.SESSION,
        ))
        return

    content = cmd.get("content", "").strip()
    if not content:
        return

    text = content
    logger.info("Text input on session %s: %s", session_id, text[:80])

    # #75 phase 1b: reset the per-turn tool-call tracker.  Each
    # incoming text starts a new turn; the `_on_tool_result`
    # callback accumulates here so the end-of-turn empty-reply
    # guard can synthesise a template wrap from what actually
    # fired.
    conn_state["tool_calls_this_turn"] = []

    # Audit B1 (#165): mark turn busy so scheduler-fired widgets
    # defer until this text turn completes — prevents the
    # `llm token / widget_card / llm token` interleave.
    if surface_mgr is not None:
        surface_mgr.mark_turn_start(session_id)
    try:
        await body_fn(ws, conn_state, cmd, text, session_id, content)
    finally:
        if surface_mgr is not None:
            try:
                await surface_mgr.mark_turn_end(session_id)
            except Exception:
                logger.exception(
                    "B1: turn-end drain failed for text turn",
                )
