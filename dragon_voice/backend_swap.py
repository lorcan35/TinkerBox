"""Pipeline + ConversationEngine LLM backend swap.

Wave 23 SOLID-audit follow-up — sixth sub-handler extract from
`_handle_config_update` (after vision_capability #214,
cap_downgrade #215, config_swap #216, config_swap_guards #217,
codec_negotiation #218).

This module owns the actual hot-swap moment: take the post-validated
config, rotate the per-connection pipeline + the shared
ConversationEngine's LLM, handle the swap-failure error path with
γ-arch error_events + revert frames.  It's the biggest remaining
inline chunk in `_handle_config_update` (~80 LOC).

## Two backends, one moment

Pre-Wave-22b (#207), the pipeline and ConvEngine swap paths drifted
— the WS dispatcher swapped both inline; the HTTP `/api/config`
handler only swapped pipelines.  Wave 22b extracted
`ConversationEngine.swap_llm` as the canonical ConvEngine-side
swap; this PR extracts the WS-side caller into a single function so
both swap surfaces happen in one place with one error-handling
policy.

## API

`await swap_pipeline_and_conversation_backends(ws, ...) -> bool`

Returns ``True`` iff the **pipeline** swap succeeded (or no pipeline
existed yet).  Returns ``False`` if `pipeline.swap_backends` raised
— in which case the γ-arch error_event + revert frames have already
been sent and the caller should short-circuit out of
`_handle_config_update`.

The ConvEngine swap failure is logged but does NOT cause `False` —
matches the pre-extract behaviour where ConvEngine swap exceptions
were caught + swallowed (since the pipeline swap is the user-visible
path; ConvEngine drift is a "next text turn might be wrong" concern,
not a "config_update flow is broken" one).

## TinkerClaw session-key injection

Pre-extract the swap loop also injected a session key onto the
post-swap pipeline LLM via `SupportsSessionKey`.  That happens here
too — the session_id comes from `conn_state["session_id"]`.

## DIP

`safe_send_json` is passed in.  `conversation` and `backend_pool`
are passed in (rather than reaching back into VoiceServer's
attributes).  The whole module compiles without importing
`VoiceServer`.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

from dragon_voice.errors import DragonError, Scope, Severity, error_event

logger = logging.getLogger(__name__)


SafeSendJson = Callable[[web.WebSocketResponse, dict], Awaitable[bool]]

# Revert payload sent on any swap failure — flips Tab5 back to LOCAL
# voice mode visually so the user understands the swap didn't take.
_REVERT_TO_LOCAL: dict = {"type": "config_update", "voice_mode": 0}


async def swap_pipeline_and_conversation_backends(
    ws: web.WebSocketResponse,
    *,
    conn_state: Any,                  # ConnState or dict
    conn_config: Any,                 # VoiceConfig
    llm_be: str,                      # post-selection LLM backend name
    voice_mode: int,                  # raw int — for ConvEngine.swap_llm router-mode
    conversation: Optional[Any],      # ConversationEngine (server._conversation)
    backend_pool: dict,               # server._backend_pool
    safe_send_json: SafeSendJson,
) -> bool:
    """Hot-swap the pipeline backends + ConversationEngine LLM.

    Args:
        ws: Open WebSocket to Tab5.
        conn_state: Per-connection state — read for `pipeline`,
            `session_id`, `ws_id`.
        conn_config: Post-validation, post-selection VoiceConfig.
        llm_be: The selected LLM backend name (from
            `select_backends_for_mode`).  Drives the TC session-key
            injection branch.
        voice_mode: Raw int form of the active mode (router uses
            `set_voice_mode(int)` to flip its tier filter).
        conversation: The shared ConversationEngine, or None during
            boot.  When non-None, its LLM is also swapped (canonical
            Wave 22b path via `swap_llm`).
        backend_pool: The shared backend instance pool that
            `swap_llm` reuses to avoid re-loading Ollama / re-opening
            aiohttp sessions on every config_update.
        safe_send_json: WS-send callable (passed in for DIP — keeps
            this module from importing VoiceServer).

    Returns:
        True if the pipeline swap succeeded (or no pipeline existed).
        False if the pipeline swap raised — caller MUST short-circuit
        out of `_handle_config_update`.  The γ-arch error_event +
        revert frames have already been sent.
    """
    # ── Pipeline swap (the user-visible failure path) ───────────
    pipeline = conn_state.get("pipeline")
    if pipeline:
        try:
            # swap_backends() handles cancel internally and sets
            # _swapping flag to drop audio during swap.
            await pipeline.swap_backends(conn_config)
            # Inject session key for TinkerClaw conversation continuity.
            # Wave 21b (#204): isinstance(SupportsSessionKey) over hasattr.
            # `pipeline._llm` is a private attribute on Pipeline; the
            # getattr-with-None still guards the rare case where swap
            # leaves it unset.
            from dragon_voice.llm.base import SupportsSessionKey
            pipe_llm = getattr(pipeline, "_llm", None)
            if llm_be == "tinkerclaw" and isinstance(pipe_llm, SupportsSessionKey):
                pipe_llm.set_session_key(conn_state.get("session_id", ""))
        except DragonError as de:
            # Already a γ-arch structured error — emit verbatim.
            logger.warning("Backend swap failed (DragonError): %s", de.message)
            await safe_send_json(ws, de.to_event())
            await safe_send_json(ws, _REVERT_TO_LOCAL)
            return False
        except Exception:
            # A4 (audit, #137): the prior code did
            # `f"Backend swap failed: {e}"` which leaked raw Python
            # exception text (e.g. the multi-line OpenRouter / TC
            # ValueError) into Tab5's voice caption.  Send a γ-arch
            # error event with a user-friendly message instead; the
            # full trace is still in the logs via logger.exception
            # below.
            logger.exception(
                "Backend swap failed for %s",
                conn_state.get("ws_id", "?"),
            )
            await safe_send_json(ws, error_event(
                code="backend_swap_failed",
                message="Couldn't switch backends — reverted to local",
                severity=Severity.FATAL,
                scope=Scope.LLM,
            ))
            await safe_send_json(ws, _REVERT_TO_LOCAL)
            return False

    # ── ConversationEngine swap (the silent-failure path) ───────
    # W15-C01: prefer the pooled instance so we don't re-load Ollama /
    # re-open the aiohttp session on every config_update.  Only the
    # pipeline owns the shutdown of a pooled backend.
    #
    # #183 PR 3: when the active backend is the capability-aware
    # router, voice_mode changes don't require a backend swap —
    # the router holds the entire fleet and just flips its tier
    # policy via set_voice_mode().  Saves the cost of recreating
    # sub-backends + losing their warm-loaded models.
    #
    # Wave 22b (#202): canonical swap via ConversationEngine.swap_llm —
    # closes the WS / HTTP swap-path divergence (HTTP path uses
    # pipeline.swap_backends; ConvEngine now has its own public swap
    # that both can call going forward).
    if conversation is not None:
        try:
            await conversation.swap_llm(
                conn_config.llm,
                pool=backend_pool,
                voice_mode=voice_mode,
            )
        except Exception as e:
            # ConvEngine swap failure is logged but does NOT cause
            # return False — pipeline swap succeeded, the user-visible
            # path works for the next voice turn; the next text turn
            # may use the stale ConvEngine LLM until the next
            # config_update fires.  Matches pre-extract behaviour.
            logger.exception("ConversationEngine LLM swap failed: %s", e)

    return True
