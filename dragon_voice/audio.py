"""Pure audio-utility helpers shared by the voice and text TTS paths.

Audit B8 (#137): pre-fix this module didn't exist; the same linear
PCM-resample block was duplicated in `pipeline._synthesize_and_send`
and in `server._handle_text`'s text-path TTS branch.  Any quality fix
to one (e.g. swapping linear interpolation for a polyphase filter)
would silently miss the other.  Centralising here means future quality
work has exactly one site to change.

Audit C8 (#137): added `resample_pcm16_async` so callers can run the
numpy work off the event loop on long replies without blocking other
WS frames (cancels, voice frames from another tab, etc.).  The sync
form stays for tests + tiny buffers where the dispatch overhead
dwarfs the work.
"""
from __future__ import annotations

import asyncio

import numpy as np

__all__ = ["resample_pcm16", "resample_pcm16_async"]


# Below this size the GIL-bound thread-dispatch round-trip costs more
# than just running synchronously.  Calibrated against ~2 KB
# (≈ 60 ms of 16 kHz audio) — a single sentence-flush chunk on
# Piper.  Empirical, not theoretical; revisit if profiling shows
# resample as a hotspot.
_ASYNC_RESAMPLE_MIN_BYTES = 2048


def resample_pcm16(audio_bytes: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linear-interpolation resample of a raw PCM int16 buffer.

    Returns the input unchanged when the rates already match (this is
    common — Piper outputs 22050 Hz to a 22050 Hz Tab5 fallback path
    and the resample would be wasted CPU + a needless allocation).

    Empty input returns empty output without raising.
    """
    if src_rate == dst_rate or not audio_bytes:
        return audio_bytes
    src = np.frombuffer(audio_bytes, dtype=np.int16)
    if src.size == 0:
        return audio_bytes
    ratio = dst_rate / src_rate
    new_len = int(src.size * ratio)
    if new_len <= 0:
        return b""
    indices = np.arange(new_len) / ratio
    idx_floor = np.clip(indices.astype(np.int32), 0, src.size - 2)
    frac = indices - idx_floor
    out = (src[idx_floor] * (1 - frac) + src[idx_floor + 1] * frac).astype(np.int16)
    return out.tobytes()


async def resample_pcm16_async(
    audio_bytes: bytes, src_rate: int, dst_rate: int
) -> bytes:
    """Async wrapper that hops the resample to a worker thread when
    the buffer is large enough that the numpy pass would block the
    event loop noticeably.

    Audit C8 (#137): pre-fix the resample ran synchronously on the
    asyncio loop.  A 2-paragraph TTS reply (~150 KB at 22 kHz) takes
    ~10-20 ms of numpy time on Q6A — long enough to delay cancel /
    ping / voice frames from other connections served by the same
    process.  The threshold below avoids dispatch overhead on small
    sentence-flush chunks.
    """
    if (
        src_rate == dst_rate
        or not audio_bytes
        or len(audio_bytes) < _ASYNC_RESAMPLE_MIN_BYTES
    ):
        return resample_pcm16(audio_bytes, src_rate, dst_rate)
    return await asyncio.to_thread(
        resample_pcm16, audio_bytes, src_rate, dst_rate
    )
