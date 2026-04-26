"""Test for C6 (#137): dictation MAX_AUDIO_BUFFER cap emits a γ-arch
event once per recording session instead of silently dropping audio.

Pre-fix the cap was a bare `logger.warning` + `return` — the user
kept dictating past 5 minutes and got a transcript that ended at the
cap with no signal anything was wrong.
"""
from __future__ import annotations

import asyncio

import pytest

from dragon_voice import pipeline as pipeline_mod
from dragon_voice.config import VoiceConfig
from dragon_voice.pipeline import VoicePipeline, MAX_AUDIO_BUFFER


def _make_pipeline() -> tuple[VoicePipeline, list[dict]]:
    cfg = VoiceConfig()
    events: list[dict] = []

    async def on_event(e: dict) -> None:
        events.append(e)

    async def on_audio(b: bytes) -> None:
        return None

    p = VoicePipeline(cfg, on_audio=on_audio, on_event=on_event)
    return p, events


def test_dictation_buffer_cap_emits_once_then_latches() -> None:
    p, events = _make_pipeline()
    p._dictation_mode = True
    # Pre-fill the segment buffer to MAX_AUDIO_BUFFER size so any
    # extra byte trips the cap.
    p._segment_buffer.extend(b"\x00" * MAX_AUDIO_BUFFER)

    async def go():
        # Three feed calls in quick succession at the cap.
        for _ in range(3):
            await p.feed_audio(b"\x00\x00")  # 2 bytes — over the cap

    asyncio.run(go())

    cap_errors = [
        e for e in events
        if e.get("type") == "error" and e.get("code") == "dictation_buffer_full"
    ]
    assert len(cap_errors) == 1, (
        f"latch should fire once per recording session, got {len(cap_errors)}"
    )
    assert cap_errors[0].get("severity") == "transient"
    assert cap_errors[0].get("scope") == "media"


def test_dictation_buffer_cap_relatches_after_cancel_session_boundary() -> None:
    """User hits cap, taps stop (cancel), restarts dictation → next
    overflow must re-emit.  The latch is per-session and `cancel()` is
    the canonical session-boundary hook."""
    p, events = _make_pipeline()
    p._dictation_mode = True
    p._segment_buffer.extend(b"\x00" * MAX_AUDIO_BUFFER)

    async def go():
        await p.feed_audio(b"\x00\x00")  # first cap event
        # User stops — `cancel()` resets the latch + clears buffers.
        await p.cancel()
        # New recording session starts; refill to cap.
        p._dictation_mode = True  # cancel() doesn't touch this
        p._segment_buffer.extend(b"\x00" * MAX_AUDIO_BUFFER)
        await p.feed_audio(b"\x00\x00")  # should emit again

    asyncio.run(go())

    cap_errors = [
        e for e in events
        if e.get("type") == "error" and e.get("code") == "dictation_buffer_full"
    ]
    assert len(cap_errors) == 2, (
        f"latch must reset on cancel(); got {len(cap_errors)} emits"
    )


def test_audio_buffer_cap_in_non_dictation_mode_emits_too() -> None:
    p, events = _make_pipeline()
    # Default mode (not dictation).
    p._audio_buffer.extend(b"\x00" * MAX_AUDIO_BUFFER)

    async def go():
        await p.feed_audio(b"\x00\x00")

    asyncio.run(go())

    cap_errors = [
        e for e in events
        if e.get("type") == "error" and e.get("code") == "audio_buffer_full"
    ]
    assert len(cap_errors) == 1


def test_under_cap_emits_nothing() -> None:
    """Regression guard: feeding audio that fits below the cap must
    NOT emit any error frame."""
    p, events = _make_pipeline()

    async def go():
        await p.feed_audio(b"\x00\x00" * 100)  # tiny chunk

    asyncio.run(go())

    cap_errors = [e for e in events if e.get("type") == "error"]
    assert cap_errors == [], f"unexpected cap emit: {cap_errors!r}"
