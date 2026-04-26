"""Test for C9 (#137): TTS fast-path flush gating switched from
LLM-backend to TTS-backend.

Pre-fix Hybrid mode (local LLM + cloud TTS) inherited the local-mode
clause-flush threshold (20 chars) and word-boundary timeout flush.
Both produced tiny chunks fed to OpenRouter TTS — choppy playback +
per-chunk network round-trip cost.

Pinning via source inspection so a refactor that reverts to the
LLM-backend gate breaks loudly.
"""
from __future__ import annotations

import inspect

from dragon_voice.pipeline import VoicePipeline


def test_fast_path_gates_on_tts_backend_not_llm() -> None:
    src = inspect.getsource(VoicePipeline._process_utterance)
    # New gating variable must exist and be derived from tts.backend.
    assert "is_local_tts = self._config.tts.backend != \"openrouter\"" in src, (
        "C9 expects is_local_tts derived from tts.backend, not llm.backend"
    )
    # The clause-flush + word-boundary branches must use the new variable.
    assert "clause_min_chars = 20 if is_local_tts else 60" in src, (
        "clause-flush gating must use is_local_tts"
    )
    assert "is_local_tts\n                    and not in_code_block" in src, (
        "word-boundary timeout flush must use is_local_tts"
    )
    # The pre-fix gating must be GONE.
    assert "is_local = self._config.llm.backend in" not in src, (
        "stale llm-backend gating still present"
    )
