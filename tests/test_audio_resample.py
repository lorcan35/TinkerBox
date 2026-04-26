"""Tests for `dragon_voice.audio.resample_pcm16` (audit B8 / #137).

The helper extracts the linear-interpolation PCM resample that was
duplicated between `pipeline._synthesize_and_send` and `server._handle_text`.
This file is the behaviour-pinning test so any future quality fix
(e.g. swapping linear interp for a polyphase filter) has a single
green-bar gate to satisfy.
"""
from __future__ import annotations

import struct

import numpy as np
import pytest

from dragon_voice.audio import resample_pcm16


def _pcm16(samples: list[int]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def test_same_rate_returns_input_unchanged() -> None:
    """No-op fast path — the byte object should be returned identically
    (no allocation, no resample) when src_rate == dst_rate."""
    buf = _pcm16([100, 200, 300, 400])
    out = resample_pcm16(buf, 16000, 16000)
    assert out is buf, "same-rate path must short-circuit without allocation"


def test_empty_input_returns_empty() -> None:
    assert resample_pcm16(b"", 22050, 16000) == b""


def test_downsample_22050_to_16000_reduces_sample_count() -> None:
    src_rate, dst_rate = 22050, 16000
    src_samples = list(range(220))  # 220 samples = 10 ms @ 22050 Hz
    out = resample_pcm16(_pcm16(src_samples), src_rate, dst_rate)
    expected_len = int(len(src_samples) * dst_rate / src_rate)
    assert len(out) == expected_len * 2  # int16 = 2 bytes/sample


def test_upsample_22050_to_24000_increases_sample_count() -> None:
    """OpenRouter TTS occasionally outputs 22050 (when fallback to Piper
    happens), Tab5 expects 16k — verify the helper handles upsample too."""
    src_rate, dst_rate = 22050, 24000
    src_samples = [100, 200, 300, 400, 500, 600, 700, 800]
    out = resample_pcm16(_pcm16(src_samples), src_rate, dst_rate)
    expected_len = int(len(src_samples) * dst_rate / src_rate)
    assert len(out) == expected_len * 2


def test_resample_preserves_endpoint_values_approximately() -> None:
    """A linear ramp should remain monotonic after resample, with the
    first sample preserved exactly (it's at index 0 in the source)."""
    src_rate, dst_rate = 22050, 16000
    src_samples = list(range(0, 22050, 100))  # 0, 100, 200, ... — monotonic ramp
    out = resample_pcm16(_pcm16(src_samples), src_rate, dst_rate)
    out_arr = np.frombuffer(out, dtype=np.int16)
    assert out_arr[0] == 0
    # Strictly non-decreasing.
    assert all(out_arr[i] <= out_arr[i + 1] + 1 for i in range(len(out_arr) - 1))


def test_resample_matches_pre_extraction_linear_formula() -> None:
    """Pin the exact algorithm so a polyphase swap is an explicit
    test-suite update.  Compares the helper's output to the inline
    formula that lived in pipeline._synthesize_and_send pre-B8."""
    src_rate, dst_rate = 22050, 16000
    src_samples = [int(np.sin(i / 4) * 10000) for i in range(64)]
    src_bytes = _pcm16(src_samples)

    # Inline reference (the pre-B8 formula).
    audio_i16 = np.frombuffer(src_bytes, dtype=np.int16)
    ratio = dst_rate / src_rate
    new_len = int(len(audio_i16) * ratio)
    indices = np.arange(new_len) / ratio
    idx_floor = np.clip(indices.astype(np.int32), 0, len(audio_i16) - 2)
    frac = indices - idx_floor
    expected = (audio_i16[idx_floor] * (1 - frac)
                + audio_i16[idx_floor + 1] * frac).astype(np.int16).tobytes()

    actual = resample_pcm16(src_bytes, src_rate, dst_rate)
    assert actual == expected


def test_extremely_short_input_does_not_crash() -> None:
    """A 1-sample input shouldn't blow up (the inline formula clipped
    indices to len-2, which would underflow for len=1)."""
    out = resample_pcm16(_pcm16([42]), 22050, 16000)
    # 1 sample @ 22050 → ratio 0.725 → new_len = 0 → empty bytes.
    assert out == b""


def test_zero_destination_rate_clamps_to_empty() -> None:
    """Defensive: dst_rate=0 would cause a divide-by-zero in ratio.
    Helper should fall through cleanly via the new_len <= 0 guard."""
    out = resample_pcm16(_pcm16([100, 200]), 22050, 0)
    # ratio = 0 → new_len = 0 → empty bytes (no exception).
    assert out == b""


# ─────────────────────────── async wrapper (audit C8 / #137)

import asyncio  # noqa: E402

from dragon_voice.audio import resample_pcm16_async  # noqa: E402


def test_async_wrapper_matches_sync_helper_for_small_buffers() -> None:
    """Below the dispatch threshold the async wrapper short-circuits
    to the sync helper — no thread hop, identical bytes."""
    src_rate, dst_rate = 22050, 16000
    src = _pcm16([int(np.sin(i / 4) * 10000) for i in range(64)])
    sync_out = resample_pcm16(src, src_rate, dst_rate)
    async_out = asyncio.run(resample_pcm16_async(src, src_rate, dst_rate))
    assert async_out == sync_out


def test_async_wrapper_matches_sync_helper_for_large_buffers() -> None:
    """Above the dispatch threshold the helper hops to a worker thread.
    Verify the bytes are identical to the sync path so dispatch doesn't
    silently corrupt audio."""
    src_rate, dst_rate = 22050, 16000
    # ~150 KB to comfortably exceed _ASYNC_RESAMPLE_MIN_BYTES (2 KB).
    samples = [int(np.sin(i / 50) * 20000) for i in range(75_000)]
    src = _pcm16(samples)
    sync_out = resample_pcm16(src, src_rate, dst_rate)
    async_out = asyncio.run(resample_pcm16_async(src, src_rate, dst_rate))
    assert async_out == sync_out


def test_async_wrapper_does_not_block_event_loop_on_large_buffer() -> None:
    """C8 contract: a large resample must allow other tasks to run on
    the loop concurrently.  We schedule a 5 ms tick task alongside the
    resample and assert the tick fires at least once during it."""
    src_rate, dst_rate = 22050, 16000
    samples = [int(np.sin(i / 50) * 20000) for i in range(150_000)]  # ~300 KB
    src = _pcm16(samples)

    ticks: list[float] = []

    async def tick():
        for _ in range(20):
            await asyncio.sleep(0.005)
            ticks.append(asyncio.get_event_loop().time())

    async def go():
        await asyncio.gather(
            resample_pcm16_async(src, src_rate, dst_rate),
            tick(),
        )

    asyncio.run(go())
    # If the resample blocked the loop, the tick task wouldn't have
    # fired its full 20 iterations during the resample window.
    assert len(ticks) == 20, (
        f"event loop appears blocked during resample; only {len(ticks)} ticks fired"
    )


def test_async_wrapper_same_rate_short_circuits() -> None:
    """Same-rate must not spin up a thread."""
    buf = _pcm16([100, 200, 300])
    out = asyncio.run(resample_pcm16_async(buf, 16000, 16000))
    assert out is buf


def test_async_wrapper_empty_input_returns_empty() -> None:
    out = asyncio.run(resample_pcm16_async(b"", 22050, 16000))
    assert out == b""
