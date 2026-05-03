"""Audio codec negotiation — handles `config_update` frames that
include an `audio_uplink_codec` field for mid-session codec swap.

Wave 23 SOLID-audit follow-up — fifth sub-handler extract from
`_handle_config_update` (after vision_capability #214,
cap_downgrade #215, config_swap #216, config_swap_guards #217).

## What this does

Tab5 may send a `config_update` with `audio_uplink_codec` to swap
the uplink codec mid-session (e.g. from a Settings toggle).  This
module:

  1. Parses the codec from the WS frame (with backward-compat
     alias `audio_codec`).
  2. Calls `pipeline.set_uplink_codec(...)` to apply it on the
     active pipeline.
  3. Sends a confirmation `config_update` echoing the codec that
     was *actually* applied.

The "actually applied" piece matters for fallback observability:
if Tab5 requests `opus` but Dragon doesn't have libopus available,
`set_uplink_codec` falls back to `pcm` and the client needs to
know.

## Failure isolation

If no pipeline is wired yet (boot race) the function is a no-op.
The `safe_send_json` callable is passed in (DIP) — same shape as
the other extracted modules.

## Why a separate module

Codec negotiation is a different axis of change than backend
selection (PR 216), validation guards (PR 217), and per-mode UX
notifications (vision/cap_downgrade).  It can grow independently
to handle Opus capability ACKs, downlink codec negotiation, etc.
without touching the WS dispatcher.

Refs: PR #173, TinkerTab #262 (initial codec-swap protocol).
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

logger = logging.getLogger(__name__)

# Type alias for the WS-send callable signature.  Matches
# VoiceServer._safe_send_json (a staticmethod with no self
# dependency).
SafeSendJson = Callable[[web.WebSocketResponse, dict], Awaitable[bool]]


def _extract_codec_from_cmd(cmd: dict) -> Optional[str]:
    """Read the codec from a config_update WS frame.  Tries the
    canonical `audio_uplink_codec` field first, then the legacy
    `audio_codec` alias for backward compat with older Tab5
    firmware.  Returns None when neither is present."""
    return cmd.get("audio_uplink_codec") or cmd.get("audio_codec")


async def maybe_swap_uplink_codec(
    ws: web.WebSocketResponse,
    *,
    cmd: dict,
    conn_state: Any,                  # ConnState (or compat dict)
    safe_send_json: SafeSendJson,
) -> None:
    """If `cmd` requests an audio_uplink_codec swap, apply it on the
    active pipeline and ACK with the codec that was actually applied.

    No-op when:
      * `cmd` carries no codec field
      * no pipeline is wired yet (boot race)
      * the WS is already closed (ACK is best-effort)

    Args:
        ws: Open WebSocket to Tab5.
        cmd: The parsed `config_update` WS frame.
        conn_state: ConnState (or compat dict) — read for `pipeline`.
        safe_send_json: VoiceServer._safe_send_json (passed in to
            preserve the WS-send retry/swallow policy without
            importing VoiceServer here).

    Returns:
        None.  All side effects flow through the pipeline + ws.
    """
    requested = _extract_codec_from_cmd(cmd)
    if requested is None:
        return

    pipeline = conn_state.get("pipeline")
    if pipeline is None:
        # Connection still booting — codec selection happens at
        # pipeline init from the conn_config; client will see the
        # final codec via session_start, no-op here.
        return

    applied = pipeline.set_uplink_codec(str(requested))
    if not ws.closed:
        await safe_send_json(ws, {
            "type": "config_update",
            "audio_uplink_codec": applied,
            "reason": "codec_negotiation",
        })
