"""TinkerClaw text-path bypass for the LLM turn.

Wave 23 SOLID-audit follow-up — fifth sub-extract from
`_handle_text_body` (round 4, after rich_media_emit #227,
empty_response_wrap #228, text_path_receipt #229,
text_path_tts #230).

When `voice_mode=3` (TinkerClaw mode) is active, Dragon delegates
the LLM call to the TinkerClaw gateway and bypasses the local
ConversationEngine.process_text_stream path entirely.  The TC
bypass branch (~100 LOC pre-extract) handles:

  * ConvEngine LLM lookup + session-key set
  * Empty "thinking" indicator (keeps WS alive while gateway
    spins up — TC agent can take 10-30s before first token)
  * Token streaming with `_ws_keepalive_during_inference` (so
    Tab5's PONG-watch doesn't trip and trigger P13 eviction)
  * γ2-M6 (#106) DragonError fast-fail with structured event
  * Empty-response wrap (W15-H09 apology fallback when no tools
    fired)
  * `llm_done` emit
  * TC zero-cost receipt (`emit_tinkerclaw_zero_cost_receipt`)
  * Rich media emit (dedup with the local-path branch)

This module is the dedicated home for that whole chain.

## API

```python
handled = await handle_tinkerclaw_text_path(
    ws,
    *,
    conn_state,
    conn_config,
    text,
    session_id,
    conversation,
    media_pipeline,
    ws_keepalive,
    safe_send_json,
) -> bool
```

Returns `True` when the TC bypass handled the turn (caller should
return).  Returns `False` when not in TC mode (caller falls
through to the local ConvEngine path).

## DIP

`ws_keepalive` is the bound method `VoiceServer._ws_keepalive_during_inference`
passed in as a callable so this module doesn't need to import or
construct the keepalive helper itself.  Same pattern used for
`safe_send_json`.

`conversation.llm` is reached through the public `.llm` property
(closed via Wave 21b ENC-1 — was `._llm` pre-#204).

## Why a precondition gate not a runtime check

The caller decides TC vs local based on `conn_config.llm.backend`
and the presence of `conversation.llm`.  The function then
re-checks the same precondition so it remains safe to call as a
"try TC first" guard without the caller duplicating the gate
logic.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

from dragon_voice.errors import DragonError
from dragon_voice.empty_response_wrap import maybe_synthesize_empty_response_wrap
from dragon_voice.rich_media_emit import emit_rich_media_for_text_turn
from dragon_voice.text_path_receipt import emit_tinkerclaw_zero_cost_receipt

logger = logging.getLogger(__name__)


SafeSendJson = Callable[[web.WebSocketResponse, dict], Awaitable[bool]]
# `ws_keepalive(ws, label=...) -> async context manager`.  Bound to
# VoiceServer._ws_keepalive_during_inference at call time.
WsKeepaliveFactory = Callable[..., Any]


# Default fallback emitted when the gateway returned no usable text
# AND no tools fired this turn.  W15-H09: gives the user a clear
# "try again" instead of a silent dropped bubble.
_TC_NO_TOOLS_FALLBACK = (
    "Sorry, I couldn't generate a response for that. "
    "Please try rephrasing, or try again in a moment."
)


# #336: TC-specific system prompt.  The OpenClaw gateway already
# injects its own skill-catalog system message, so this stays short:
# it just establishes that the caller is the Tab5 voice assistant on
# behalf of a recurring user, and reminds the agent to use whatever
# memory / location / preferences are already known rather than
# treating each turn as a blank slate.  The actual conversation
# history (from MessageStore.get_context) carries the per-turn
# context.
_TC_SYSTEM_PROMPT = (
    "You are TinkerClaw, the voice assistant running on a Tab5 device. "
    "The user talks to you across many turns and reconnects — treat "
    "this as a continuing conversation with the same person.  Use any "
    "memory, skills, location, or preferences you already know about "
    "them before asking them to repeat themselves.  Keep replies short "
    "and friendly; the user hears them spoken aloud."
)
# Max prior turns from MessageStore to forward to the gateway.  20 is
# what local ConvEngine uses by default; we mirror that.
_TC_HISTORY_MAX = 20


def _is_tinkerclaw_mode(conn_config: Any, conversation: Any) -> bool:
    """Return True iff this turn should bypass ConvEngine and go
    through the TinkerClaw gateway.

    Two conditions: voice_mode 3 wired the LLM as `tinkerclaw` AND
    ConversationEngine has a live `.llm` reference (post-swap race
    safety — `pipeline._llm` may be stale after a hot-swap).
    """
    if not conn_config:
        return False
    if conn_config.llm.backend != "tinkerclaw":
        return False
    if conversation is None or conversation.llm is None:
        return False
    return True


async def handle_tinkerclaw_text_path(
    ws: web.WebSocketResponse,
    *,
    conn_state: dict,
    conn_config: Any,                # VoiceConfig (Optional in test paths)
    text: str,
    session_id: str,
    conversation: Optional[Any],     # ConversationEngine (Optional)
    media_pipeline: Optional[Any],   # MediaPipeline (Optional in test paths)
    ws_keepalive: WsKeepaliveFactory,
    safe_send_json: SafeSendJson,
    message_store: Optional[Any] = None,  # MessageStore — #336 context restore
) -> bool:
    """Run the TinkerClaw bypass branch of `_handle_text_body`.

    Returns True when the TC path handled the turn (caller returns
    immediately).  Returns False when not in TC mode (caller
    proceeds to the local ConvEngine path).

    On gateway fast-fail (DragonError from the pre-flight health
    check, γ2-M6 / #106), emits the structured γ1 error_event +
    `llm_done` so Tab5 surfaces a FATAL/GATEWAY banner instead of
    waiting the full 600 s sock_read timeout.

    Behaviour preserved verbatim from the pre-extract chain:
      * `set_session_key` only when LLM implements SupportsSessionKey
      * Empty `llm` frame as "thinking" indicator
      * `_ws_keepalive_during_inference(label="tc_text")` wraps the
        token stream
      * Empty-response wrap with TC apology fallback (W15-H09)
      * TC zero-cost receipt (`emit_tinkerclaw_zero_cost_receipt`)
      * Rich media emit (only when LLM produced any tokens)
    """
    if not _is_tinkerclaw_mode(conn_config, conversation):
        return False

    # ENC-1 (audit 2026-05-03): goes through the public `.llm`
    # property instead of the private `_llm` attribute.  Behavior
    # unchanged — the property is a thin pass-through.
    llm = conversation.llm
    logger.info("_handle_text TinkerClaw bypass via ConvEngine LLM: %s", llm.name)

    # Wave 21b (#204): isinstance(SupportsSessionKey) over hasattr.
    # #336: use device_id (stable across reconnects) instead of
    # session_id (rotates per WS connect).  Falls back to session_id
    # if no device_id was registered on this connection so test paths
    # and pre-#336 deployments still work.
    from dragon_voice.llm.base import SupportsSessionKey
    if isinstance(llm, SupportsSessionKey):
        stable_key = (
            conn_state.get("device_id", "")
            or conn_state.get("session_id", "")
        )
        llm.set_session_key(stable_key)

    # Send a "thinking" indicator immediately to keep the WS alive.
    # TinkerClaw agent can take 10-30s before first token (memory
    # recall, skill execution).  Without this, ngrok kills the
    # idle connection.
    if not ws.closed:
        await ws.send_json({"type": "llm", "text": ""})

    # #336: persist the user turn + build a context-aware message
    # list from MessageStore.  Pre-#336 the gateway only saw the
    # latest user message, so it had no idea what the user said in
    # earlier turns or what they previously told the system.
    # MessageStore persistence is best-effort — if no store was
    # wired (test path) we fall back to the legacy single-turn
    # context.
    messages_for_gateway: list[dict]
    if message_store is not None and session_id:
        try:
            await message_store.add_message(
                session_id, "user", text, input_mode="text",
            )
        except Exception:
            logger.exception(
                "#336: persisting user turn failed — continuing "
                "with single-message context",
            )
        try:
            messages_for_gateway = await message_store.get_context(
                session_id,
                max_messages=_TC_HISTORY_MAX,
                system_prompt=_TC_SYSTEM_PROMPT,
            )
        except Exception:
            logger.exception(
                "#336: get_context failed — falling back to "
                "single-message context",
            )
            messages_for_gateway = [
                {"role": "system", "content": _TC_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ]
    else:
        messages_for_gateway = [
            {"role": "system", "content": _TC_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ]

    # #75 phase 1a: WS-level PING every 5 s while the LLM is
    # generating.  5 s is safely under every Tab5 firmware's
    # PONG-watch window (30–45 s); the outer
    # `WebSocketResponse(heartbeat=60)` timer is too slow for a
    # 90 s 4 B-class Ollama turn.  See docs/AUDIT.md "Local-mode
    # gauntlet" for the observed P13-eviction race.
    full_response: list[str] = []
    try:
        async with ws_keepalive(ws, label="tc_text"):
            async for token in llm.generate_stream_with_messages(
                messages_for_gateway,
            ):
                full_response.append(token)
                if not ws.closed:
                    await ws.send_json({"type": "llm", "text": token})
    except DragonError as e:
        # γ2-M6 (issue #106): TC gateway pre-flight health check
        # failed in ≤ 5 s.  Emit the structured γ1 error frame so
        # Tab5 can surface a FATAL/GATEWAY banner — pre-fix the
        # user waited the full 600 s sock_read timeout before the
        # connection-error fallback fired.
        logger.warning(
            "TC text path fast-failed: %s (code=%s)",
            e.message, e.code,
        )
        if not ws.closed:
            await safe_send_json(ws, e.to_event())
            await ws.send_json({
                "type": "llm_done", "llm_ms": 0, "text": "",
            })
        return True

    response_text = "".join(full_response)

    # SOLID-audit follow-up: empty-response guard.
    # TC semantics: pass fallback_when_no_tools so the W15-H09
    # apology fires when zero tools fired this turn.
    response_text = await maybe_synthesize_empty_response_wrap(
        ws,
        response_text=response_text,
        tool_calls=conn_state.get("tool_calls_this_turn") or [],
        safe_send_json=safe_send_json,
        log_label="tc",
        fallback_when_no_tools=_TC_NO_TOOLS_FALLBACK,
    )

    logger.info(
        "TinkerClaw text response (%d chars): %s",
        len(response_text), response_text[:80],
    )

    # #336: persist the assistant turn so the next call's
    # get_context() sees it.  Best-effort.
    if message_store is not None and session_id and response_text:
        try:
            await message_store.add_message(
                session_id, "assistant", response_text,
                input_mode="text",
                model=getattr(llm, "name", "tinkerclaw"),
            )
        except Exception:
            logger.exception(
                "#336: persisting assistant turn failed — next "
                "turn will be missing this exchange",
            )

    if not ws.closed:
        await ws.send_json({
            "type": "llm_done", "llm_ms": 0, "text": response_text,
        })

    # SOLID-audit follow-up: TC zero-cost receipt.
    # Same model-name resolution priority chain
    # (_model → name → config_default → minimax fallback) is
    # pinned by the 4 dedicated tests in test_text_path_receipt.py.
    await emit_tinkerclaw_zero_cost_receipt(
        ws,
        llm=llm,
        tinkerclaw_model_default=getattr(
            conn_config.llm, "tinkerclaw_model", "",
        ) or "",
        safe_send_json=safe_send_json,
    )

    # SOLID-audit follow-up: rich media detection (dedup with the
    # local-path branch).  Only fires when the LLM actually
    # produced tokens — avoids a no-op render when the gateway
    # short-circuited.
    if full_response:
        await emit_rich_media_for_text_turn(
            ws,
            response_text=response_text,
            media_pipeline=media_pipeline,
            session_id=session_id,
            safe_send_json=safe_send_json,
            log_label="tc",
        )

    return True
