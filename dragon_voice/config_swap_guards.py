"""Config-update prerequisite validation guards.

Wave 23 SOLID-audit follow-up — fourth sub-handler extract from
`_handle_config_update` (after vision_capability #214, cap_downgrade
#215, config_swap #216).

This module owns the three pre-swap validation guards that must pass
before a `config_update` actually rotates backends:

  * **TinkerClaw gateway health** — if vmode==TINKERCLAW, hit the
    gateway's `/health` endpoint and confirm it returns 200 within 5 s.
  * **OpenRouter API key presence** — if vmode needs cloud STT/TTS or
    LLM, confirm `conn_config.llm.openrouter_api_key` is non-empty.
  * **TinkerClaw token presence** — if vmode==TINKERCLAW, confirm
    `conn_config.llm.tinkerclaw_token` is non-empty (otherwise the
    backend's `__init__` would raise ValueError mid-swap and the
    user would see a stack trace instead of a clean revert).

All three guards share the same failure shape: log the error, send a
γ-arch `error_event` WS frame, send a `voice_mode=0` revert
config_update so Tab5 visually flips back to LOCAL, then signal
"caller should short-circuit out of `_handle_config_update`."

## API

A single async entry point `await validate_config_swap_prereqs(...)`
returns ``True`` if all guards passed, ``False`` if any guard failed
(in which case the WS error + revert frames have already been sent).
The caller's contract is just:

    if not await validate_config_swap_prereqs(ws, vmode, conn_config,
                                              safe_send_json):
        return

This replaces ~60 LOC of inline guard chain.

## Why a single entry point

The three guards are short-circuit linked: TC-gateway → OR-key →
TC-token.  Pre-extract the caller had three identical-shaped
``if ...: return`` blocks; collapsing into one entry point removes
the repeated short-circuit pattern and makes the caller body
expressive about intent ("validate prerequisites or bail").

## DIP

The `safe_send_json` callable is passed in rather than imported.
This keeps `config_swap_guards` from depending on `VoiceServer` —
the WS-send retry/swallow policy lives on the server, the validation
policy lives here.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

import aiohttp
from aiohttp import web

from dragon_voice.errors import Scope, Severity, error_event
from dragon_voice.voice_modes import VoiceMode

logger = logging.getLogger(__name__)

# Type alias for the WS-send callable signature.  Matches
# VoiceServer._safe_send_json — a staticmethod with no self dependency.
SafeSendJson = Callable[[web.WebSocketResponse, dict], Awaitable[bool]]


# Per-failure payloads.  Centralised here so Tab5 sees a stable code
# string per failure class even if the human-readable message gets
# tweaked.
_REVERT_PAYLOAD: dict = {"type": "config_update", "voice_mode": 0}


async def _check_tinkerclaw_gateway(
    ws: web.WebSocketResponse,
    conn_config: Any,
    safe_send_json: SafeSendJson,
) -> bool:
    """Hit the TinkerClaw gateway's /health endpoint with a 5 s timeout.
    Returns True on 200, False on any other status / exception (and
    sends the WS error + revert frames).
    """
    try:
        tc_url = (conn_config.llm.tinkerclaw_url or "http://localhost:18789").rstrip("/")
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5),
        ) as tc_session:
            async with tc_session.get(f"{tc_url}/health") as tc_resp:
                if tc_resp.status != 200:
                    logger.error("TinkerClaw health check returned %d", tc_resp.status)
                    raise RuntimeError(f"health check returned {tc_resp.status}")
        logger.info("TinkerClaw gateway health OK at %s", tc_url)
        return True
    except Exception as tc_err:
        logger.error("TinkerClaw gateway not reachable: %s", tc_err)
        # Audit G5 (2026-04-20): revert to Local so Tab5 doesn't sit
        # wedged on mode 3 showing an error.
        # Audit D5 (#137): γ-arch error_event + plain config_update revert.
        if not ws.closed:
            await safe_send_json(ws, error_event(
                code="tc_gateway_unreachable",
                message="TinkerClaw gateway is not reachable — reverted to local.",
                severity=Severity.FATAL,
                scope=Scope.GATEWAY,
            ))
            await safe_send_json(ws, _REVERT_PAYLOAD)
        return False


async def _check_openrouter_key(
    ws: web.WebSocketResponse,
    conn_config: Any,
    safe_send_json: SafeSendJson,
) -> bool:
    """If the configured llm has no openrouter_api_key, send the
    γ-arch revert frames and return False."""
    if conn_config.llm.openrouter_api_key:
        return True
    logger.error("Cloud mode requested but no API key configured")
    if not ws.closed:
        await safe_send_json(ws, error_event(
            code="openrouter_key_missing",
            message="OpenRouter key not configured — reverted to local.",
            severity=Severity.FATAL,
            scope=Scope.LLM,
        ))
        await safe_send_json(ws, _REVERT_PAYLOAD)
    return False


async def _check_tinkerclaw_token(
    ws: web.WebSocketResponse,
    conn_config: Any,
    safe_send_json: SafeSendJson,
) -> bool:
    """B7 (audit, #137): TC mode needs a token; without one the
    backend's __init__ raises ValueError, which would leak via the
    A4 raw-exception path in the swap loop.  Validate up-front like
    the OpenRouter key check above so the user sees a clean γ-arch
    error and a clean revert instead of a stack trace."""
    if (conn_config.llm.tinkerclaw_token or "").strip():
        return True
    logger.error("TC mode requested but tinkerclaw_token is blank")
    if not ws.closed:
        await safe_send_json(ws, error_event(
            code="tc_token_missing",
            message="TinkerClaw token not configured — reverted to local",
            severity=Severity.FATAL,
            scope=Scope.GATEWAY,
        ))
        await safe_send_json(ws, _REVERT_PAYLOAD)
    return False


async def validate_config_swap_prereqs(
    ws: web.WebSocketResponse,
    *,
    vmode: VoiceMode,
    conn_config: Any,                  # VoiceConfig
    safe_send_json: SafeSendJson,
) -> bool:
    """Validate all prerequisites for switching to ``vmode``.

    Returns ``True`` iff every applicable guard passed.  Returns
    ``False`` if any guard failed — the failing guard has already
    sent the WS error_event + revert config_update frames; the
    caller's only obligation is to short-circuit out of
    ``_handle_config_update``.

    Guard execution order:
      1. TC gateway health (only when vmode==TINKERCLAW).
      2. OpenRouter API key presence (only when vmode needs OR key).
      3. TC token presence (only when vmode==TINKERCLAW).

    The TC-gateway check is first because a dead gateway is the
    most common failure in dev (forgot to start tinkerclaw-gateway
    service); the user gets the most-likely-relevant error message
    fastest.
    """
    if vmode.is_tinkerclaw():
        if not await _check_tinkerclaw_gateway(ws, conn_config, safe_send_json):
            return False

    if vmode.needs_openrouter_key():
        if not await _check_openrouter_key(ws, conn_config, safe_send_json):
            return False

    if vmode.is_tinkerclaw():
        if not await _check_tinkerclaw_token(ws, conn_config, safe_send_json):
            return False

    return True
