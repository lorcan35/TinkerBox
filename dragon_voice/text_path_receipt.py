"""Per-turn `receipt` emission for the text-path LLM turns.

Wave 23 SOLID-audit follow-up — third sub-extract from
`_handle_text_body` (round 4, after rich_media_emit #227 and
empty_response_wrap #228).

The text-path receipts (vs voice-path receipts emitted from
`pipeline._process_utterance`) are sent by `_handle_text_body`
right after the LLM finishes streaming.  Two flavours:

  * **Local path** — pulls token counts + cost from the active
    LLM via the `SupportsUsage` protocol (Wave 21b PR #204) and
    `price_for_model` (PR #210), then sends a `receipt` frame.
  * **TinkerClaw bypass path** — TC bills to its own gateway so
    Dragon doesn't have token counts; emits a zero-cost receipt
    with just the model name so the chat bubble still gets
    stamped ("claw-agent · FREE") instead of no stamp at all.

This module is the dedicated home for both.

## API

```python
await emit_text_path_llm_receipt(
    ws,
    *,
    conversation,
    safe_send_json,
) -> None
```

Local-path receipt.  Reads token usage from `conversation.llm`
(via `SupportsUsage`), computes cost via `price_for_model`,
sends an `llm`-stage receipt.  Failures swallowed with WARNING
log — receipt is informational, not session correctness.

```python
await emit_tinkerclaw_zero_cost_receipt(
    ws,
    *,
    llm,
    tinkerclaw_model_default,
    safe_send_json,
) -> None
```

TC-specific receipt.  Picks the model name (private `_model`
first, then `.name`, then config default, then hardcoded
`minimax/MiniMax-M2.5` last-resort) and sends a zero-cost
receipt frame.

## Design choices

  * Both functions accept `safe_send_json` even though the
    pre-extract code mostly used `ws.send_json` directly.  The
    audit-D6-style transport-close swallow benefit applies to
    these receipts too, even if the original code path was
    "send and pray (with try/except)".
  * The TC receipt's `_model` reach-through is preserved for
    behavioural parity, with a TODO pointing at a future
    `TinkerClawBackend.public_model_name()` accessor (audit
    follow-up to ENC-1).
  * The local receipt's `convo._llm` access is also preserved
    pending a follow-up `ConversationEngine.get_last_usage()`
    accessor (sister of `choose_vision_model` in PR #212).
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

logger = logging.getLogger(__name__)


SafeSendJson = Callable[[web.WebSocketResponse, dict], Awaitable[bool]]


# Last-resort fallback when neither the live LLM instance nor the
# config carries a model name.  Matches Wave 8 audit #2 (A4/F3/J12)
# pre-fix behaviour: the first turn after registration would
# otherwise stamp the bubble with the bare string "tinkerclaw".
_TC_MODEL_LAST_RESORT = "minimax/MiniMax-M2.5"


async def emit_text_path_llm_receipt(
    ws: web.WebSocketResponse,
    *,
    conversation: Optional[Any],     # ConversationEngine (Optional for tests)
    safe_send_json: SafeSendJson,
) -> None:
    """Emit a `receipt` frame for the just-completed local
    text-path LLM turn.

    Voice-path receipts come from `pipeline._process_utterance`;
    the text path reaches the LLM via ConversationEngine
    directly and bypasses the pipeline entirely, so we emit
    here too.

    No-op when:
      * `conversation` is None (test paths, embedded usage).
      * The live LLM doesn't implement `SupportsUsage` (e.g.
        backends like `dual` or `tinkerclaw` use a different
        receipt path — see `emit_tinkerclaw_zero_cost_receipt`).
      * `get_last_usage()` returns an empty / zero-token dict.

    Failure isolation: any exception in the lookup or send
    chain is caught + logged at ERROR with traceback.  A failed
    receipt must NOT block the next text turn.
    """
    try:
        if conversation is None:
            return
        # ENC-1 follow-up TODO: this still reaches into the
        # private `_llm` attribute.  A future
        # `ConversationEngine.get_last_usage()` accessor would
        # close the SRP smell.  Behaviour-preserving for now.
        cur_llm = getattr(conversation, "_llm", None)
        if cur_llm is None:
            return

        # Wave 21b (#204): isinstance(SupportsUsage) over hasattr —
        # closes the "model='llm'" silent fallback for backends
        # like `dual` and `tinkerclaw` that lacked
        # get_last_usage entirely.
        from dragon_voice.llm.base import SupportsUsage
        if not isinstance(cur_llm, SupportsUsage):
            return

        usage = cur_llm.get_last_usage()
        if not usage or not usage.get("total_tokens"):
            return

        from dragon_voice.llm.openrouter_llm import price_for_model
        cost_mils = price_for_model(
            usage["model"],
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
        )

        if not ws.closed:
            await safe_send_json(ws, {
                "type":              "receipt",
                "stage":             "llm",
                "model":             usage["model"],
                "prompt_tokens":     usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens":      usage.get("total_tokens", 0),
                "cost_mils":         cost_mils,
                # v4·D Gauntlet G2: surface retries so the chat
                # bubble can render a "RETRIED" chip when the
                # OpenRouter retry path fired (context_trim or
                # 429 backoff).
                "retried":           bool(usage.get("retried", False)),
                "retry_reason":      usage.get("retry_reason", ""),
            })
        logger.info(
            "Receipt emitted (text): model=%s tok=%d+%d=%d cost_mils=%d",
            usage["model"],
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            usage.get("total_tokens", 0),
            cost_mils,
        )
    except Exception:
        logger.exception("Text-path receipt emit failed")


async def emit_tinkerclaw_zero_cost_receipt(
    ws: web.WebSocketResponse,
    *,
    llm: Any,
    tinkerclaw_model_default: str,
    safe_send_json: SafeSendJson,
) -> None:
    """v4·D connectivity polish: emit a zero-cost receipt on the
    TinkerClaw bypass path so the chat bubble gets stamped
    ("claw-agent · FREE") instead of no stamp at all.

    TC turns don't expose token counts the way OpenRouter does;
    we just surface the engine name so transparency-per-bubble
    still holds.

    ## Model-name resolution priority

    Wave 8 audit #2 (A4/F3/J12) — the first-turn fallback used
    to be the bare string "tinkerclaw" which shows up in chat
    bubbles as a generic stamp until the gateway populates
    `_model`.  Fall through this priority chain so the first
    bubble stamp is still honest:

      1. `llm._model` — the actual model the gateway loaded
      2. `llm.name`   — the backend's display name
      3. `tinkerclaw_model_default` — config default
      4. `minimax/MiniMax-M2.5` — last-resort

    Failure isolation: any send exception is logged at DEBUG
    (not ERROR — receipt is purely informational on the TC path
    where there's no token cost to surface).
    """
    if ws.closed:
        return
    try:
        # ENC-1 follow-up TODO: `_model` is a private attr.
        # Behaviour-preserving for now; a future
        # TinkerClawBackend.public_model_name() accessor would
        # close the SRP smell.
        inner = getattr(llm, "_model", "") or ""
        tc_model = (
            inner
            or getattr(llm, "name", None)
            or tinkerclaw_model_default
            or _TC_MODEL_LAST_RESORT
        )
        await safe_send_json(ws, {
            "type":             "receipt",
            "stage":            "llm",
            "model":            tc_model,
            "prompt_tokens":    0,
            "completion_tokens": 0,
            "total_tokens":     0,
            "cost_mils":        0,          # TC bills to its own gateway
            "llm_ms":           0,
            "retried":          False,
            "retry_reason":     "",
        })
    except Exception:
        logger.debug("TC receipt emit failed", exc_info=True)
