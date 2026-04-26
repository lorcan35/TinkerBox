"""Pure audio-utility helpers shared by the voice and text TTS paths.

Audit B8 (#137): pre-fix this module didn't exist; the same linear
PCM-resample block was duplicated in `pipeline._synthesize_and_send`
and in `server._handle_text`'s text-path TTS branch.  Any quality fix
to one (e.g. swapping linear interpolation for a polyphase filter)
would silently miss the other.  Centralising here means future quality
work has exactly one site to change.
"""
from __future__ import annotations

import numpy as np

__all__ = ["resample_pcm16"]


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
