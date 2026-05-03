"""Voice-mode → backend selection (the heart of `_handle_config_update`).

2026-05-03 SOLID audit follow-up — third sub-handler extraction from
`_handle_config_update`.  Sister of:

  * `vision_capability.emit_vision_capability` (PR #214)
  * `cap_downgrade.maybe_speak_cap_downgrade_alert` (PR #215)

This module owns the mapping from `(VoiceMode, llm_model_request) →
(stt_backend, tts_backend, llm_backend)` and the matching mutation of
`conn_config.llm` (model id + mode-aware system prompt + max_tokens).
The validation paths (TC health check, OpenRouter key check, TC token
check) stay in `server.py` for now — they need to send WS error frames
and early-return out of the handler, which is harder to factor out
without growing a "skip the rest of config_update" return code.

## API surface

A single function `select_backends_for_mode(vmode, conn_config,
llm_model=None) -> BackendSelection` returns the chosen triple.  It
mutates `conn_config.llm` in place to apply mode-aware overrides
(system prompt, max tokens, model id).  Both behaviours match the
pre-extract code byte-for-byte.

## Why a dataclass return value

The pre-extract code wrote into three local variables (`stt_be`,
`tts_be`, `llm_be`) and used them ~5 times further down in
`_handle_config_update`.  Returning a dataclass keeps that surface
typed without forcing the caller to thread three positional args.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from dragon_voice.config import (
    SYSTEM_PROMPT_CLOUD,
    SYSTEM_PROMPT_HYBRID,
    SYSTEM_PROMPT_LOCAL,
    MAX_TOKENS_CLOUD,
    MAX_TOKENS_HYBRID,
    MAX_TOKENS_LOCAL,
)
from dragon_voice.voice_modes import VoiceMode

logger = logging.getLogger(__name__)


@dataclass
class BackendSelection:
    """Result of `select_backends_for_mode`.  Holds the three backend
    names that were chosen.  Side effects on `conn_config.llm` (model
    id + system_prompt + max_tokens overrides) happened during the
    selection — see the function docstring."""

    stt_backend: str
    tts_backend: str
    llm_backend: str


def select_backends_for_mode(
    vmode: VoiceMode,
    conn_config: Any,                  # VoiceConfig — Any to dodge cycle
    llm_model: Optional[str] = None,
) -> BackendSelection:
    """Resolve STT/TTS/LLM backend names for `vmode` and apply
    mode-aware overrides to `conn_config.llm` in place.

    ## Backend triple per VoiceMode

    | Mode | STT | TTS | LLM |
    |------|-----|-----|-----|
    | LOCAL | moonshine | piper | local_backend (default ollama) |
    | HYBRID | openrouter | openrouter | local_backend (default ollama) |
    | CLOUD | openrouter | openrouter | openrouter |
    | TINKERCLAW | moonshine* | piper* | tinkerclaw |
    | ONBOARD | (Tab5-side only — never reaches Dragon) | — | — |

    *TinkerClaw with `"cloud"` in the requested `llm_model` overrides
    its STT/TTS to OpenRouter (matches Tab5's voice_mode=3 + cloud
    suffix UX).

    ## conn_config.llm mutations

    * `tinkerclaw_model` — set when vmode==TINKERCLAW and `llm_model`
      is non-empty.
    * `openrouter_model` — set when vmode==CLOUD and `llm_model` is
      non-empty.
    * `ollama_model` — set when local-tier + `llm_model` is a plain
      identifier (no `/` — vendor-prefix means cloud override).
    * `system_prompt` + `max_tokens` — flipped per VoiceMode tier
      (LOCAL/HYBRID/CLOUD); TINKERCLAW skips because the gateway
      manages its own personality.

    ## Wire-protocol invariant

    Pre-extract behaviour is preserved bit-for-bit.  This is a pure
    refactor to give the mode→backend mapping its own home + tests.

    Args:
        vmode: The active VoiceMode for the connection.
        conn_config: Per-connection VoiceConfig (mutated in place).
        llm_model: Optional model id from the WS frame.  Empty / None
            means "keep the configured default for this tier".

    Returns:
        BackendSelection with the three chosen backend names.
    """
    # ── STT + TTS ───────────────────────────────────────────────
    if vmode.is_local():
        stt_be, tts_be = "moonshine", "piper"
    elif vmode.is_tinkerclaw():
        # TinkerClaw mode: default local STT/TTS.
        # "cloud" suffix in llm_model → use OpenRouter STT/TTS.
        if llm_model and "cloud" in llm_model.lower():
            stt_be, tts_be = "openrouter", "openrouter"
        else:
            stt_be, tts_be = "moonshine", "piper"
    else:
        stt_be, tts_be = "openrouter", "openrouter"

    # ── LLM ─────────────────────────────────────────────────────
    if vmode.is_tinkerclaw():
        # TinkerClaw mode — gateway handles everything.
        llm_be = "tinkerclaw"
        if llm_model:
            conn_config.llm.tinkerclaw_model = llm_model
    elif vmode.is_cloud():
        llm_be = "openrouter"
        if llm_model:
            conn_config.llm.openrouter_model = llm_model
    else:
        llm_be = conn_config.llm.local_backend or "ollama"
        if llm_model and llm_be == "ollama" and "/" not in llm_model:
            conn_config.llm.ollama_model = llm_model
            logger.info("Local model switched to: %s", llm_model)

    # ── Mode-aware system prompt + max_tokens ───────────────────
    # TINKERCLAW: skip — TinkerClaw owns personality + token budget.
    if vmode.is_tinkerclaw():
        pass
    elif vmode.is_local():
        conn_config.llm.system_prompt = SYSTEM_PROMPT_LOCAL
        conn_config.llm.max_tokens = MAX_TOKENS_LOCAL
    elif vmode.is_hybrid():
        conn_config.llm.system_prompt = SYSTEM_PROMPT_HYBRID
        conn_config.llm.max_tokens = MAX_TOKENS_HYBRID
    else:
        conn_config.llm.system_prompt = SYSTEM_PROMPT_CLOUD
        conn_config.llm.max_tokens = MAX_TOKENS_CLOUD

    return BackendSelection(stt_backend=stt_be, tts_backend=tts_be, llm_backend=llm_be)
