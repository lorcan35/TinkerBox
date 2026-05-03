"""Vision-capability advertisement — emits the `vision_capability`
WS frame after each `config_update` so Tab5's camera screen knows
which model (if any) will handle a vision turn at the current voice
mode, and what each frame will cost in mils.

2026-05-03 SOLID audit follow-up: extracted from
`server.py:_handle_config_update` (~80 LOC).  Pre-extract this lived
inline at the bottom of the 446-LOC handler, tangled with the
config_update ACK + cap_downgrade speak-system trigger.  The three
sub-responsibilities — config swap, vision advertise, cap-downgrade
speak — change for different reasons (router fleet shape vs Tab5
chip rendering vs cap policy) and shouldn't share a method.

This module is the first step of the Wave 22a `WsDispatcher`-style
decomposition: pull self-contained sub-responsibilities into
purpose-specific modules so the giant handlers can shrink to thin
coordinators.

## Routing strategy

Two paths handle vision-capability detection:

1. **Router path** — when the active backend is a
   `CapabilityAwareRouter`, ask `ConversationEngine.choose_vision_model`
   for the spec it would pick.  Already covers ~12 cloud vendors via
   `_PRICING_MILS_PER_M` (see OCP-2, PR #210) plus any local-tier
   vision model in the fleet.

2. **Single-backend fallback** — when the router isn't active,
   substring-match the configured model id against the known vision
   vendor list.  This list IS still hardcoded; promoting it to the
   centralized capability registry is a separate audit follow-up
   (OCP-1 sibling on the read path).

Both paths route per-frame pricing through `vision_per_frame_mils()`
so adding a vendor to the registry automatically gets correct mils.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from aiohttp import web

from dragon_voice.llm.openrouter_llm import vision_per_frame_mils
from dragon_voice.voice_modes import VoiceMode

logger = logging.getLogger(__name__)


# Substring hints for the single-backend (non-router) fallback.
# Matches a vendor's marker substring against the configured model id;
# any match means "this model handles vision".
#
# Kept here, not in voice_modes, because it's specifically about the
# *current* OpenRouter cloud model registry — a different axis of
# change than VoiceMode tier semantics.  Add new vision-capable
# OpenRouter vendors here when they ship.
_VISION_HINT_SUBSTRINGS_CLOUD: tuple[str, ...] = (
    "gpt-4o", "sonnet", "haiku", "gemini",
    "opus", "grok", "kimi", "qwen3.6", "glm", "mimo",
)

# Local-mode (Ollama) vision hints — same shape as the cloud list.
_VISION_HINT_SUBSTRINGS_LOCAL: tuple[str, ...] = ("vision", "llava")


async def emit_vision_capability(
    ws: web.WebSocketResponse,
    *,
    conversation: Optional[Any],   # ConversationEngine — Any to dodge cycle
    vmode: VoiceMode,
    conn_config: Any,              # VoiceConfig — Any to dodge cycle
    active_model: str,
) -> None:
    """Send a `vision_capability` WS frame to Tab5.

    Always emits exactly one message (or zero if `ws.send_json` raises);
    never raises out to the caller.  All exceptions are logged + eaten
    so a config_update flow doesn't get torn down by a render failure
    on a chip that's purely informational.

    Args:
        ws: Open WebSocket to Tab5.  Caller must check `ws.closed`
            before invoking.
        conversation: The active ConversationEngine, or None during
            boot.  When the engine's LLM is a `CapabilityAwareRouter`,
            its `choose_vision_model(voice_mode)` returns the spec
            the router would pick — used here to populate the chip.
        vmode: The active VoiceMode (post-config_update).
        conn_config: Per-connection VoiceConfig — read for the
            single-backend fallback's openrouter_model / ollama_model
            substring match.
        active_model: The post-swap LLM model id Tab5 sees on the
            config_update ACK.  Used as the substring-fallback
            display name when no router pick is available.
    """
    vision_model = ""
    per_frame_mils = 0

    # Router path — preferred when an active fleet exists.
    spec = (
        conversation.choose_vision_model(int(vmode))
        if conversation is not None else None
    )
    if spec is not None:
        vision_model = spec.model_id
        # Local-tier sub-backends are free regardless of the canonical
        # pricing table (which is OR-only); short-circuit to avoid a
        # `_default`-table surprise on a local vision model id.
        per_frame_mils = (
            0 if spec.tier == "local"
            else vision_per_frame_mils(spec.model_id)
        )
    else:
        # Single-backend fallback — substring match on the configured
        # model id.  Pricing routes through the centralized helper so
        # adding a vendor to the substring list automatically gets
        # correct mils via _PRICING_MILS_PER_M.
        if vmode.is_cloud():
            or_model_lc = conn_config.llm.openrouter_model.lower()
            if any(h in or_model_lc for h in _VISION_HINT_SUBSTRINGS_CLOUD):
                vision_model = active_model
                per_frame_mils = vision_per_frame_mils(
                    conn_config.llm.openrouter_model,
                )
        elif vmode.is_local():
            om = conn_config.llm.ollama_model.lower()
            if any(h in om for h in _VISION_HINT_SUBSTRINGS_LOCAL):
                vision_model = active_model
                per_frame_mils = 0  # local Ollama vision = no $ cost

    try:
        await ws.send_json({
            "type":           "vision_capability",
            "can_see":        bool(vision_model),
            "model":          vision_model,
            "per_frame_mils": per_frame_mils,
        })
    except Exception:
        logger.exception("vision_capability emit failed")
