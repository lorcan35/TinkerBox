"""Config-update ACK message builder.

Wave 23 SOLID-audit follow-up — seventh sub-handler extract from
`_handle_config_update` (after vision_capability #214,
cap_downgrade #215, config_swap #216, config_swap_guards #217,
codec_negotiation #218, backend_swap #219).

Sends Tab5 the post-swap `config_update` confirmation message
containing the active backend triple + the active model name +
(when the capability-aware router is active) the per-modality
fleet summary so Tab5's camera screen can light up its
capability chips dynamically.

## API surface

```python
active_model = await emit_config_update_ack(
    ws,
    vmode=vmode,
    conn_config=conn_config,
    backends=sel,
    conversation=self._conversation,
    safe_send_json=self._safe_send_json,
)
```

Returns the resolved `active_model` string so downstream steps
(`vision_capability.emit_vision_capability`) can reuse it without
recomputing — that helper needs it for the substring-fallback
display name when no router pick is available.

## Active-model resolution rules

| LLM backend | Source field |
|-------------|--------------|
| openrouter (`vmode.is_cloud()`) | `conn_config.llm.openrouter_model` |
| tinkerclaw | `conn_config.llm.tinkerclaw_model` |
| ollama | `conn_config.llm.ollama_model` |
| anything else | `""` |

The `vmode.is_cloud()` check is paranoia: pre-extract the code
gated this branch on `voice_mode == 2` (now `vmode.is_cloud()` —
PR #211); the LLM backend name "openrouter" is the same predicate
in normal flow but using the typed mode keeps the intent
self-describing.

## Wire format

```json
{
  "type": "config_update",
  "config": {
    "stt": "moonshine",
    "tts": "piper",
    "llm": "ollama",
    "llm_model": "ministral-3:3b",
    "voice_mode": 0,
    "cloud_mode": false,
    "fleet_summary": {...}        // only present when router active
  }
}
```

## Failure isolation

`safe_send_json` is the WS-send callable passed in for DIP — same
shape as the other extracted modules.  When the WS is closed,
the active_model is still computed + returned (so downstream
callers don't get an empty string) but the actual send is
skipped.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

from dragon_voice.config_swap import BackendSelection
from dragon_voice.voice_modes import VoiceMode

logger = logging.getLogger(__name__)


SafeSendJson = Callable[[web.WebSocketResponse, dict], Awaitable[bool]]


def _resolve_active_model(
    vmode: VoiceMode,
    conn_config: Any,
    llm_backend: str,
) -> str:
    """Pick the user-visible active-model string for the ACK payload.

    Pure function — no I/O, no mutation.  Exposed for direct use by
    callers that need the resolved name without sending the ACK
    (e.g. tests).
    """
    if vmode.is_cloud():
        return conn_config.llm.openrouter_model or ""
    if llm_backend == "tinkerclaw":
        return conn_config.llm.tinkerclaw_model or ""
    if llm_backend == "ollama":
        return conn_config.llm.ollama_model or ""
    return ""


async def emit_config_update_ack(
    ws: web.WebSocketResponse,
    *,
    vmode: VoiceMode,
    conn_config: Any,
    backends: BackendSelection,
    conversation: Optional[Any],   # ConversationEngine — Any to dodge cycle
    safe_send_json: SafeSendJson,
) -> str:
    """Send the post-swap config_update confirmation to Tab5.

    Returns the resolved active_model string so callers can reuse
    it without recomputing (the vision_capability emit downstream
    needs it for the substring-fallback display name).

    When `ws.closed`, returns the active_model but skips the send —
    the downstream caller still gets a usable string.
    """
    active_model = _resolve_active_model(vmode, conn_config, backends.llm_backend)

    if ws.closed:
        return active_model

    config_payload: dict = {
        "stt": backends.stt_backend,
        "tts": backends.tts_backend,
        "llm": backends.llm_backend,
        "llm_model": active_model,
        "voice_mode": int(vmode),
        # cloud_mode is the legacy binary toggle; True for any mode
        # that touches cloud (Hybrid/Cloud/TC).  Onboard is Tab5-side-
        # only and never reaches Dragon.
        "cloud_mode": int(vmode) >= 1,
    }

    # #183 PR 3 + Wave 22b (#202): when the capability-aware router is
    # active, advertise the per-modality fleet summary so Tab5
    # firmware can light up vision/video/audio capability chips
    # dynamically.  Single-backend configurations get a None back
    # from `fleet_summary()` and the field is omitted (Tab5 firmware
    # falls back to the legacy `vision_capability` event for
    # backward-compat).
    if conversation is not None:
        summary = conversation.fleet_summary(int(vmode))
        if summary is not None:
            config_payload["fleet_summary"] = summary

    await safe_send_json(ws, {
        "type": "config_update",
        "config": config_payload,
    })

    return active_model
