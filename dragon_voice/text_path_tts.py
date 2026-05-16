"""TTS synthesis for the text-path LLM turns.

Wave 23 SOLID-audit follow-up — fourth sub-extract from
`_handle_text_body` (round 4, after rich_media_emit #227,
empty_response_wrap #228, text_path_receipt #229).

The text-path TTS chunk (~80 LOC pre-extract) handles the
voice rendering of an LLM text reply: backend selection,
mode-aware timeout budget, async resample, paced byte
streaming, and the F5 TTS receipt + the audit-L3
zombie-Piper-kill on timeout.

This module is the dedicated home for that whole chain.

## API

```python
await synthesize_and_stream_text_response(
    ws,
    *,
    pipeline,
    response_text,
    response_mode,
    conn_config,
    safe_send_json,
) -> None
```

No-op when:
  * No pipeline (boot race).
  * No `pipeline._tts` (pipeline initialised without TTS).
  * Response text is whitespace-only.
  * `ws` is closed.
  * `response_mode == "match_input"` (text-in / text-out
    semantics — Tab5 told Dragon NOT to speak).

All exceptions in the synth + send chain are caught.  The
finally branch ALWAYS sends `tts_end` so Tab5 doesn't hang in
SPEAKING state — even on transport-close mid-stream.

## Mode-aware timeout budget

Per audit P1: Piper can take 15-25 s on Q6A ARM64 for a
200-word reply; cloud gpt-audio-mini is fast but still needs a
cushion when OpenRouter edge adds latency.

  * Piper / kokoro / edge_tts (local) → 90 s budget
  * OpenRouter cloud → 30 s budget

## Audit L3 zombie-Piper kill on timeout

#89 Phase 2 L3: kill any in-flight Piper subprocess before
bailing out of an asyncio.TimeoutError.  Without this the
text path leaks the zombie until Python exits — same class of
bug as A1 (cancel) but on the timeout edge.  Mirrors the
voice-path pattern at `pipeline.py:1411-1421`.

## DIP

`pipeline._tts` is still reached through the private attribute
to match pre-extract behaviour.  The _tts surface area
(synthesize, sample_rate, kill_active_procs) is informally
public-API-shaped already; a future
`VoicePipeline.tts_backend` property would close the SRP smell
(audit follow-up).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

from dragon_voice.audio import resample_pcm16_async

logger = logging.getLogger(__name__)


SafeSendJson = Callable[[web.WebSocketResponse, dict], Awaitable[bool]]


# Mode-aware TTS synth budget (seconds).  Cloud is fast but still
# needs a cushion for OpenRouter edge latency; local backends can
# take 15-25 s on Q6A ARM64 for a 200-word reply.
_TTS_TIMEOUT_LOCAL_S = 90
_TTS_TIMEOUT_CLOUD_S = 30

# Streaming chunk size for the WS audio byte stream.  Chosen so
# the per-chunk pace_sleep below stays under any practical
# WebSocket buffer threshold.
_TTS_CHUNK_BYTES = 4096


def _resolve_tts_timeout(tts_backend: str) -> int:
    """Pick the synth-budget timeout for the active TTS backend.

    Cloud (openrouter) gets the tighter 30 s budget; everything
    else (piper, kokoro, edge_tts) gets the 90 s local budget.
    """
    return _TTS_TIMEOUT_CLOUD_S if tts_backend == "openrouter" else _TTS_TIMEOUT_LOCAL_S


async def synthesize_and_stream_text_response(
    ws: web.WebSocketResponse,
    *,
    pipeline: Optional[Any],         # VoicePipeline (Optional for boot race)
    response_text: str,
    response_mode: str,
    conn_config: Any,                # VoiceConfig (Optional in test paths)
    safe_send_json: SafeSendJson,
) -> None:
    """Synthesize TTS for an LLM text response and stream the
    audio bytes back to Tab5, then emit the F5 TTS receipt.

    No-op when any of the precondition guards fail — see module
    docstring.

    On synth timeout / failure: kills any in-flight Piper
    subprocess (audit L3) so the zombie doesn't leak, sends
    `tts_end` with `tts_ms=0` so Tab5 doesn't hang in SPEAKING
    state.
    """
    # Precondition guards (mirror pre-extract chain):
    if pipeline is None:
        return
    tts = getattr(pipeline, "_tts", None)
    if tts is None:
        return
    if not response_text.strip():
        return
    if ws.closed:
        return
    if response_mode == "match_input":
        # Tab5 asked for text-in / text-out — don't speak.
        return

    # Resolve the active TTS backend name from conn_config.
    tts_backend = conn_config.tts.backend if conn_config else "piper"
    tts_timeout = _resolve_tts_timeout(tts_backend)

    try:
        # Tell Tab5 the spoken reply is starting.
        await ws.send_json({"type": "tts_start"})
        t0 = time.monotonic()

        # #338 follow-up: strip markdown/bullets/code-fences/emojis
        # BEFORE handing the text to any backend — otherwise Kokoro
        # cheerfully reads "asterisk asterisk Geneva asterisk asterisk"
        # out loud.  Was only wired into the voice-mic path
        # (pipeline.py) and the REST /synthesize path (api/synthesize.py)
        # in #339 — this is the third call site that synthesizes user-
        # visible LLM replies, used for TEXT-INPUT TC turns.
        cleaner_enabled = (
            getattr(conn_config.tts, "text_cleaner_enabled", True)
            if conn_config is not None else True
        )
        synth_text = response_text
        if cleaner_enabled:
            from dragon_voice.tts import clean_for_tts
            cleaned = clean_for_tts(response_text)
            if cleaned:
                synth_text = cleaned

        audio_bytes = await asyncio.wait_for(
            tts.synthesize(synth_text),
            timeout=tts_timeout,
        )
        tts_ms = (time.monotonic() - t0) * 1000

        if audio_bytes:
            # Audit B8 + C8 (#137): shared async resample with the
            # voice-path TTS branch.  Long replies (~150 KB) hop to a
            # worker thread so this WS read loop stays free for
            # cancels / other frames.
            tts_rate = tts.sample_rate
            target_rate = conn_config.audio.input_sample_rate if conn_config else 16000
            audio_bytes = await resample_pcm16_async(
                audio_bytes, tts_rate, target_rate,
            )

            # Pace the byte stream so we don't dump 150 KB into a
            # 4-byte WebSocket buffer at once.  Sleep ~0.8x the
            # actual audio duration of the chunk so the receive
            # buffer stays fed without backing up.
            pace_sleep = (_TTS_CHUNK_BYTES / 2) / target_rate * 0.8
            for i in range(0, len(audio_bytes), _TTS_CHUNK_BYTES):
                chunk = audio_bytes[i:i + _TTS_CHUNK_BYTES]
                if not ws.closed:
                    await ws.send_bytes(chunk)
                if i > _TTS_CHUNK_BYTES * 3:
                    await asyncio.sleep(pace_sleep)

        if not ws.closed:
            await ws.send_json({"type": "tts_end", "tts_ms": round(tts_ms)})
            # Audit F5 (2026-04-20): TTS receipt for text-path
            # synthesis so chat bubbles surface the TTS backend
            # that spoke the reply.
            try:
                await safe_send_json(ws, {
                    "type": "receipt",
                    "stage": "tts",
                    "model": tts_backend,
                    "tts_ms": round(tts_ms),
                    "cost_mils": 0,
                })
            except Exception:
                # Receipt is informational; don't break the WS path.
                pass

    except (asyncio.TimeoutError, Exception) as tts_err:
        # #89 Phase 2 L3: kill any in-flight Piper subprocess
        # before bailing.  Without this the text path leaks
        # the zombie until Python exits — same class of bug
        # as A1 (cancel) but on the timeout edge.  Mirrors
        # the voice-path pattern at pipeline.py:1411-1421.
        if hasattr(tts, "kill_active_procs"):
            try:
                tts.kill_active_procs()
            except Exception:
                logger.debug("kill_active_procs raised", exc_info=True)
        if isinstance(tts_err, asyncio.TimeoutError):
            logger.warning(
                "Text-path TTS timed out after %ds — killed Piper procs",
                tts_timeout,
            )
        else:
            logger.exception("TTS for text input failed")
        # Always send tts_end so Tab5 doesn't hang in SPEAKING.
        if not ws.closed:
            await ws.send_json({"type": "tts_end", "tts_ms": 0})
