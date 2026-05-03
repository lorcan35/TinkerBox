"""Binary WS frame dispatcher — VID0 / AUD0 / raw PCM routing.

Wave 23 SOLID-audit follow-up — tenth sub-extract from the
WS-handler family in server.py (round 4 spillover, after the
nine prior extracts #227-#235).

Tab5 sends three flavours of binary frames over /ws/voice:

  1. **VID0-tagged** — call-mode video frames (#175).  4-byte
     magic header `"VID0"` + JPEG payload.  Routed to the video
     relay handler which broadcasts to other connected clients.
  2. **AUD0-tagged** — call-mode audio frames (#181 / TinkerTab
     #272).  4-byte magic header `"AUD0"` + raw int16 PCM (or
     OPUS).  Broadcast verbatim to peers; bypasses STT.
  3. **Raw PCM** — the legacy unprefixed mic stream.  Falls
     through to `pipeline.feed_audio()`.

Pre-extract this 50-LOC dispatcher lived inline at the top of
the `_handle_ws_voice` message loop.  Now lives here so the
loop body in `server.py` stays focused on the protocol surface.

## API

```python
await dispatch_binary_frame(
    msg_data,
    *,
    conn_state,
    active_connections,
)
```

Three terminal cases:
  * VID0 → forwarded to `video_upstream.get_handler().on_frame`
  * AUD0 → broadcast to peer connections sharing a different
           session_id, with per-peer drop on send failure
  * Raw  → forwarded to `pipeline.feed_audio`

The dispatcher is intentionally I/O-only (no state mutation on
conn_state); side effects are all on the WS or the pipeline.

## Why per-peer try/except on AUD0 broadcast

A single dead peer connection in the fan-out shouldn't tear
down the relay for every other listener.  Pre-extract the
exception swallow was at DEBUG so call sessions stayed clean
even when one Tab5 dropped.

## Why no `pipeline` parameter

`pipeline` is read fresh from `conn_state` per-frame because
config-update mid-call may have hot-swapped the pipeline
backend.  Capturing it at handler-entry would point at a stale
backend after a swap.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def dispatch_binary_frame(
    msg_data: bytes,
    *,
    conn_state: dict,
    active_connections: dict,
) -> None:
    """Dispatch a single binary WS frame to the right handler.

    Routing is by 4-byte magic prefix:

      * ``"VID0"`` → video relay (`video_upstream.on_frame`)
      * ``"AUD0"`` → call-audio fan-out to peer connections
      * other     → `pipeline.feed_audio` (raw PCM mic stream)

    All branches are no-ops when the prerequisite is missing
    (no pipeline attached, no peers connected) so this function
    is safe to call before / during / after the register handshake.
    """
    # Local imports preserve the pre-extract behaviour (these
    # were imported inside the message loop, not at module top).
    # Keeps test imports lightweight too — tests can stub these
    # without paying the import cost up-front.
    from dragon_voice.video_upstream import get_handler as _video_get_handler
    from dragon_voice.video_upstream import parse_video_frame
    from dragon_voice.audio_codec import peek_call_audio_magic

    # ── #175: VID0-tagged video frame ─────────────────────
    if parse_video_frame.peek(msg_data):
        await _video_get_handler().on_frame(
            session_id=conn_state.get("session_id", ""),
            device_id=conn_state.get("device_id", ""),
            wire_bytes=msg_data,
            active_connections=active_connections,
        )
        return

    # ── #181 / TinkerTab #272: AUD0 call-audio fan-out ─────
    if peek_call_audio_magic(msg_data):
        sender_sid = conn_state.get("session_id", "")
        for c in list(active_connections.values()):
            if c.get("session_id") == sender_sid:
                continue
            peer_ws = c.get("ws")
            if peer_ws is None or peer_ws.closed:
                continue
            try:
                await peer_ws.send_bytes(msg_data)
            except Exception as e:
                # One dead peer in the fan-out shouldn't tear the
                # relay down for everyone else.  DEBUG so live
                # call sessions don't spam ops at WARNING when a
                # peer Tab5 drops mid-call.
                logger.debug("call-audio relay drop: %s", e)
        return

    # ── Legacy raw PCM mic stream → pipeline ───────────────
    # Note: feed_audio is NOT locked (US-P10) — it only appends
    # to the audio buffer and the VAD check is lightweight.  The
    # heavy processing (_process_utterance) is triggered via
    # asyncio.create_task inside feed_audio and that task is
    # serialized by the pipeline's own _processing flag.  Locking
    # here would block audio ingestion during LLM/TTS processing.
    pipeline: Any = conn_state.get("pipeline")
    if pipeline:
        await pipeline.feed_audio(msg_data)
