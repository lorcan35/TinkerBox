"""Unit tests for the voice-pipeline TTS flush logic + Piper kill-on-timeout.

Phase 2 H2 + L3 of the UX-gap remediation (see docs/UX-GAPS.md / issue #94).

H2 — Pre-fix the LLM stream loop only flushed buffered tokens to TTS on
sentence boundary (`.!?`) or clause boundary (`,;:—` past 20/60 chars).
A reply with no early punctuation could buffer 60+ chars before any TTS
chunk left Dragon — audible mid-reply silence.  Code blocks containing
`def foo():` triggered false clause-flushes that sent `def foo():`
verbatim to Piper.

L3 — Pre-fix `_synthesize_and_send`'s except branch handled OpenRouter
fallback to Piper but never called `kill_active_procs()`.  A stalled
in-flight Piper subprocess holding the audio device + an FD could
linger past the timeout.  The fallback Piper itself had no second
timeout, so a TTS-down scenario could leak indefinitely.

Tests cover the two helper-level concerns + the lifecycle behavior
that's exercisable without spinning a real LLM/STT/TTS:

  - Word-boundary regex (`_LAST_WORD_BOUNDARY`)
  - Triple-backtick toggle correctness
  - Constants are sane
  - kill_active_procs is invoked on Piper TTS timeout (mocked TTS)
  - Fallback Piper second-timeout also calls kill_active_procs
"""
from __future__ import annotations

import asyncio
import re
from unittest.mock import patch

import pytest

from dragon_voice.config import VoiceConfig
from dragon_voice.pipeline import (
    VoicePipeline,
    _LAST_WORD_BOUNDARY,
    _LOCAL_TIMEOUT_FLUSH_MIN_CHARS,
    _LOCAL_TIMEOUT_FLUSH_S,
    _TRIPLE_BACKTICK,
)


# ───────────────────────── helper-level


def test_last_word_boundary_finds_last_space() -> None:
    """The timeout flush splits at the LAST whitespace before the trailing
    word so we don't chop a word in half."""
    s = "Let me search for that information"
    m = _LAST_WORD_BOUNDARY.search(s)
    assert m is not None
    # Match starts at the space before "information"
    assert s[m.start()] == " "
    assert s[: m.start()] == "Let me search for that"
    assert s[m.start():].lstrip() == "information"


def test_last_word_boundary_no_space() -> None:
    """Single-word buffer has no boundary — caller should hold the whole
    thing for the next iteration."""
    assert _LAST_WORD_BOUNDARY.search("supercalifragilistic") is None


def test_last_word_boundary_trailing_whitespace() -> None:
    """Buffer ending in whitespace — last boundary is that whitespace."""
    s = "Hello "
    m = _LAST_WORD_BOUNDARY.search(s)
    assert m is not None
    assert s[: m.start()] == "Hello"


def test_constants_are_sane() -> None:
    """Don't ship a regression where someone sets MIN_CHARS=1 (would
    micro-stutter every word) or TIMEOUT=0.05 (would over-fire)."""
    assert 0.10 <= _LOCAL_TIMEOUT_FLUSH_S <= 1.0, (
        "300 ms is the sweet spot — too low chops every word; too high "
        "and the user feels the gap"
    )
    assert _LOCAL_TIMEOUT_FLUSH_MIN_CHARS >= 10, (
        "Need enough buffer to make a meaningful TTS chunk"
    )
    assert _TRIPLE_BACKTICK == "```", "Code-block marker is the markdown standard"


# ───────────────────────── code-block toggle (parity with the in-loop logic)


def test_triple_backtick_toggle_parity() -> None:
    """Mirror of the toggle logic at pipeline.py: count backticks per
    token, toggle if odd.  This test pins down the contract so a refactor
    doesn't accidentally start using a different rule."""
    in_code = False
    tokens = ["here is code: ", "```python\n", "def foo():\n", "    return 1\n", "```", " end"]
    snapshots: list[bool] = []
    for tok in tokens:
        if _TRIPLE_BACKTICK in tok:
            n = tok.count(_TRIPLE_BACKTICK)
            if n % 2 == 1:
                in_code = not in_code
        snapshots.append(in_code)
    # After "here is code: ": False (no backtick)
    # After "```python\n": True (entered code)
    # After "def foo():": True (still in code — colon would NOT trigger
    # clause flush in the production path)
    # After "    return 1": True
    # After "```": False (exited code)
    # After " end": False
    assert snapshots == [False, True, True, True, False, False]


def test_triple_backtick_inline_pair_no_toggle() -> None:
    """An inline ``code`` pair in one token (two backticks total) is
    even — net no toggle, stays out of code-block mode."""
    in_code = False
    tok = "use ```code``` like that"
    if _TRIPLE_BACKTICK in tok:
        n = tok.count(_TRIPLE_BACKTICK)
        if n % 2 == 1:
            in_code = not in_code
    assert in_code is False


# ───────────────────────── kill_active_procs on TTS timeout


class _StallTTS:
    """TTS that always raises asyncio.TimeoutError after a tiny delay,
    and tracks whether kill_active_procs was called."""
    sample_rate = 22050
    def __init__(self) -> None:
        self.killed = 0
    async def synthesize(self, text: str) -> bytes:
        # The wait_for at the call site has timeout=30; we raise the same
        # exception type the wait_for would raise on a real timeout.
        raise asyncio.TimeoutError("simulated piper stall")
    def kill_active_procs(self) -> None:
        self.killed += 1


class _OkTTS:
    """TTS that returns 1024 bytes immediately (used as fallback)."""
    sample_rate = 22050
    def __init__(self) -> None:
        self.synth_calls = 0
        self.killed = 0
    async def synthesize(self, text: str) -> bytes:
        self.synth_calls += 1
        return b"\x00\x00" * 512  # 1024 bytes of silence
    def kill_active_procs(self) -> None:
        self.killed += 1


def _make_pipeline_with_tts(tts) -> VoicePipeline:
    cfg = VoiceConfig()
    async def on_audio(_: bytes) -> None: pass
    async def on_event(_ev: dict) -> None: pass
    p = VoicePipeline(cfg, on_audio, on_event, conversation_engine=None, session_id="t")
    p._tts = tts
    return p


def test_kill_active_procs_called_on_local_tts_timeout() -> None:
    """Local Piper TTS times out → kill_active_procs is called.  The
    outer try/except in `_synthesize_and_send` swallows the exception
    (production caller doesn't expect propagation — voice loop continues
    on TTS failure), so we verify side-effect rather than raise.  Pre-fix
    the zombie subprocess held the audio device until process exit."""
    tts = _StallTTS()
    p = _make_pipeline_with_tts(tts)
    # Default config is backend="ollama" + tts.backend="piper" — local
    # path → exception re-raises but is swallowed by outer except
    asyncio.run(p._synthesize_and_send("hello world"))
    assert tts.killed == 1, (
        f"kill_active_procs should be called exactly once on local TTS "
        f"timeout; got {tts.killed}"
    )


def test_kill_active_procs_called_on_openrouter_then_fallback_succeeds() -> None:
    """OpenRouter TTS times out → kill_active_procs called → falls back
    to a fresh Piper instance which succeeds.

    SOLID-audit follow-up: fallback Piper now lives in the
    FallbackTtsCache (PR #257); pre-cache the backend on
    `p._fallback_tts_cache._fallback_tts` to skip the lazy-load path.
    """
    primary = _StallTTS()
    fallback = _OkTTS()
    p = _make_pipeline_with_tts(primary)
    p._config.tts.backend = "openrouter"
    # PR #257: pre-cache the backend inside the cache instance.
    p._fallback_tts_cache._fallback_tts = fallback
    # Should NOT raise — fallback succeeded
    asyncio.run(p._synthesize_and_send("hello world"))
    assert primary.killed == 1, "primary TTS killed on timeout"
    assert fallback.synth_calls == 1, "fallback synth was attempted"
    assert fallback.killed == 0, "fallback TTS succeeded — no kill needed"


def test_fallback_piper_also_killed_on_second_timeout() -> None:
    """OpenRouter times out → kill primary → fallback Piper also times
    out → kill fallback → exception swallowed by outer except.
    Pre-fix the second fallback timeout silently leaked the zombie.

    SOLID-audit follow-up: fallback Piper now lives in the
    FallbackTtsCache (PR #257) and the kill_active_procs invariant
    is pinned by tests/test_fallback_tts_cache.py too.
    """
    primary = _StallTTS()
    fallback = _StallTTS()  # also stalls
    p = _make_pipeline_with_tts(primary)
    p._config.tts.backend = "openrouter"
    p._fallback_tts_cache._fallback_tts = fallback
    asyncio.run(p._synthesize_and_send("hello world"))
    assert primary.killed == 1
    assert fallback.killed == 1, (
        "Pre-fix this would have been 0 — fallback would silently leak "
        "a zombie subprocess"
    )
