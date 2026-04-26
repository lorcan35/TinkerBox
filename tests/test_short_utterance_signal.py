"""Test for C2 (#137): short-utterance audio buffer drop emits a
γ-arch event instead of silently swallowing the user's tap.

Pre-fix: any audio buffer < 1600 bytes (~50 ms) was silently dropped
via `logger.debug` + `return` when the user tapped stop.  No Tab5
frame.  Tap mic → say nothing → tap stop → no signal anything was
captured or ignored.

Post-fix: bumped the threshold to 4000 bytes (~250 ms) AND emit a
TRANSIENT/STT toast when there's some audio captured but not enough
to transcribe meaningfully.  Empty buffer (no user input at all)
still drops silently.
"""
from __future__ import annotations

import asyncio

import pytest

from dragon_voice.config import VoiceConfig
from dragon_voice.pipeline import VoicePipeline


def _make_pipeline() -> tuple[VoicePipeline, list[dict]]:
    cfg = VoiceConfig()
    events: list[dict] = []

    async def on_event(e: dict) -> None:
        events.append(e)

    async def on_audio(b: bytes) -> None:
        return None

    p = VoicePipeline(cfg, on_audio=on_audio, on_event=on_event)
    return p, events


def test_too_short_audio_with_some_bytes_emits_toast() -> None:
    """User tapped + released too fast (some audio, but < 250 ms)
    must surface a `stt_too_short` event."""
    p, events = _make_pipeline()
    # 100 bytes = ~3 ms — well under the 4000-byte threshold.
    p._audio_buffer.extend(b"\x00\x01" * 50)

    asyncio.run(p.start_processing())

    too_short = [
        e for e in events
        if e.get("type") == "error" and e.get("code") == "stt_too_short"
    ]
    assert len(too_short) == 1, (
        f"expected exactly one stt_too_short event, got {events!r}"
    )
    assert too_short[0].get("severity") == "transient"
    assert too_short[0].get("scope") == "stt"


def test_empty_buffer_does_not_emit_toast() -> None:
    """Empty buffer = no user input at all (mic disabled / aborted
    before record) — no toast.  Audit goal is to surface accidental
    short taps, not a generic 'no audio' which would spam the WS."""
    p, events = _make_pipeline()
    # Buffer untouched — empty.

    asyncio.run(p.start_processing())

    assert events == [], f"empty buffer should drop silently, got {events!r}"


def test_audio_above_threshold_does_not_emit_toast() -> None:
    """Audio above the threshold proceeds to STT — no toast."""
    p, events = _make_pipeline()
    # 5000 bytes = ~312 ms — above the 4000-byte threshold.
    p._audio_buffer.extend(b"\x00\x01" * 2500)

    # Run start_processing — it'll spawn _process_with_timeout but
    # since _stt is None it'll fail quickly.  We only care that the
    # too-short path didn't fire.
    try:
        asyncio.run(p.start_processing())
    except Exception:
        pass

    too_short = [
        e for e in events
        if e.get("type") == "error" and e.get("code") == "stt_too_short"
    ]
    assert too_short == [], (
        f"above-threshold audio must not emit stt_too_short: {events!r}"
    )


def test_threshold_is_pinned_at_4000_bytes() -> None:
    """Source-pin the new threshold.  Audit C2 calibrated to ~250 ms;
    a refactor that drifts it back to 1600 (50 ms) reopens the
    silent-drop window for accidental taps."""
    import inspect
    src = inspect.getsource(VoicePipeline.start_processing)
    assert "_MIN_AUDIO_BYTES = 4000" in src, (
        "C2 expects _MIN_AUDIO_BYTES = 4000 (~250 ms); refactor must "
        "keep this or update audit + this test together"
    )
    # The 4000 must be the operative comparison.  Allow "1600" only
    # as a documentation reference to the pre-fix value.
    assert "len(self._audio_buffer) < 1600" not in src, (
        "stale operative comparison `< 1600` still present"
    )


def test_buffer_is_cleared_after_short_drop() -> None:
    """Defensive: the buffer must NOT keep accumulating leftover
    short-tap audio across taps.  Without clearing, a user who taps
    mic 5 times in a row could eventually exceed the threshold with
    a stitched-together garbage buffer."""
    p, events = _make_pipeline()
    p._audio_buffer.extend(b"\x00\x01" * 50)

    asyncio.run(p.start_processing())

    assert len(p._audio_buffer) == 0, (
        f"buffer must be cleared after short-drop, got {len(p._audio_buffer)} bytes"
    )
