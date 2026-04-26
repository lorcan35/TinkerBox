"""Test for C3 (#137): voice-path TTS timeout matches text-path's
mode-aware 90 s / 30 s budget.

Pre-fix the voice path used a flat 30 s, which clipped long Piper
sentences (15-25 s for a 200-word reply on Q6A ARM64) — the timeout
fired before the synthesis completed and the user heard silence.

Pinning via source inspection so a refactor that drifts the value
back to a flat 30 s breaks this test loudly rather than silently
re-introducing the regression.
"""
from __future__ import annotations

import inspect

from dragon_voice.pipeline import VoicePipeline


def test_voice_path_uses_mode_aware_tts_timeout() -> None:
    src = inspect.getsource(VoicePipeline._synthesize_and_send)
    # The new computation:
    assert "tts_timeout = 30 if self._config.tts.backend == \"openrouter\" else 90" in src, (
        "C3 expects mode-aware tts_timeout (30 cloud / 90 local); refactor "
        "must keep this or update the audit + this test together"
    )
    # And the call must use the variable, not the old flat 30:
    assert "timeout=tts_timeout" in src, "primary synth must use tts_timeout"


def test_fallback_piper_timeout_is_90s() -> None:
    src = inspect.getsource(VoicePipeline._synthesize_and_send)
    # The fallback path was a hardcoded 30 s; bumped to 90 s to match
    # the new mode-aware budget for the primary local path.
    assert "self._fallback_tts.synthesize(text), timeout=90" in src, (
        "C3 expects fallback Piper timeout=90 (was 30)"
    )
