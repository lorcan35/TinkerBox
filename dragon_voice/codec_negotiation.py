"""Audio codec negotiation — register-time + mid-session paths.

Wave 23 SOLID-audit follow-up.  Two sibling functions:

  * `negotiate_uplink_codec_at_register` — initial pick from the
    capabilities list Tab5 advertises in its `register` frame.
    Picks the best mutual codec (e.g. opus when both sides
    support it) and tells Tab5 if the result is non-default.
    Originally PR #173 / TinkerTab #262; extracted from
    `_handle_register` in the round-4 spillover (PR #241).

  * `maybe_swap_uplink_codec` — handles `config_update` frames
    with an `audio_uplink_codec` field for mid-session codec
    swap (e.g. from a Settings toggle).  Originally PR #218
    sub-extract from `_handle_config_update`.

Both share the "ACK with the codec that was actually applied"
contract — if Tab5 requests `opus` but Dragon falls back to
`pcm` (libopus missing), the client needs to know.

## Failure isolation

* `negotiate_uplink_codec_at_register`: any exception in the
  whole chain (import, negotiate, set, send) is logged at
  EXCEPTION but never re-raised — codec negotiation is a
  voice-quality optimisation, not session-correctness.  The
  pipeline starts on PCM if the negotiation fails.
* `maybe_swap_uplink_codec`: silent no-op when no pipeline is
  wired yet (boot race) or no codec is in the cmd.

The `safe_send_json` callable is passed in (DIP) — same shape
as the other extracted modules.

## Why a separate module

Codec negotiation is a different axis of change than backend
selection (PR #216), validation guards (PR #217), and per-mode
UX notifications (vision/cap_downgrade).  It can grow
independently to handle Opus capability ACKs, downlink codec
negotiation, etc. without touching the WS dispatcher.

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


async def negotiate_uplink_codec_at_register(
    ws: web.WebSocketResponse,
    *,
    pipeline: Any,                       # VoicePipeline
    capabilities: Optional[dict],        # `register.capabilities` block
    device_id: str,
    safe_send_json: SafeSendJson,
) -> None:
    """Pick the best mutual uplink codec from Tab5's capability
    advertisement and apply it to the pipeline.  Sends Tab5 a
    `config_update` frame ONLY when the chosen codec is
    non-default (i.e. not "pcm") so legacy clients without the
    capability stay on PCM with no extra round-trip.

    Tab5's `register` frame may include
    `capabilities.audio_codec = ["pcm", "opus"]`.  We pick the
    best mutual one via `audio_codec.negotiate_uplink`, apply
    via `pipeline.set_uplink_codec`, and ACK with the codec that
    was *actually* applied (which may differ from the request
    if libopus is missing on Dragon).

    No-op when:
      * `capabilities` is None or not a dict.
      * `capabilities["audio_codec"]` is missing or not a list.
      * The chosen codec is "pcm" (default — no client switch
        needed).
      * The WS is closed (the ACK is best-effort).

    Failure isolation: any exception in the whole chain is
    logged at EXCEPTION but never re-raised — codec negotiation
    is a voice-quality optimisation, not session-correctness.
    The pipeline starts on PCM if anything blows up.
    """
    try:
        client_codecs = (
            capabilities.get("audio_codec")
            if isinstance(capabilities, dict) else None
        )
        if not isinstance(client_codecs, list):
            return

        # Local import preserves the pre-extract pattern (lazy
        # so the audio_codec module's import cost only fires when
        # a client actually sends the capability).
        from dragon_voice import audio_codec as _ac

        chosen = _ac.negotiate_uplink(client_codecs)
        applied = pipeline.set_uplink_codec(chosen)
        logger.info(
            "Audio codec negotiation %s: client=%s chosen=%s applied=%s",
            device_id, client_codecs, chosen, applied,
        )
        if applied != "pcm" and not ws.closed:
            await safe_send_json(ws, {
                "type": "config_update",
                "audio_uplink_codec": applied,
                "reason": "codec_negotiation",
            })
    except Exception:
        logger.exception("audio codec negotiation failed (non-fatal)")


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
