"""Tests for ``dragon_voice.speak_system.speak_system_message``.

Pin the bracket invariant (snapshot _tts_started, run synth,
conditionally emit tts_end), the failure-isolation guarantees,
and the no-op preconditions.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.speak_system import speak_system_message


def _make_pipeline(
    *,
    has_tts: bool = True,
    initial_tts_started: bool = False,
    initial_tts_total_ms: float = 0.0,
    synth_starts_tts: bool = True,
    synth_raises: Exception | None = None,
) -> SimpleNamespace:
    """Build a minimal pipeline-shaped stub.

    `synth_starts_tts=True` simulates `_synthesize_and_send`
    flipping `_tts_started` to True (the typical first-utterance
    path).  When False, the synth runs without flipping the flag
    (e.g. mid-utterance call).
    """
    pipeline = SimpleNamespace(
        _tts=MagicMock() if has_tts else None,
        _tts_started=initial_tts_started,
        _tts_total_ms=initial_tts_total_ms,
        _on_event=AsyncMock(),
    )

    async def _synth(text):
        if synth_raises:
            raise synth_raises
        if synth_starts_tts:
            pipeline._tts_started = True
            pipeline._tts_total_ms += 250  # simulate accumulator

    pipeline._synthesize_and_send = AsyncMock(side_effect=_synth)
    return pipeline


# ─── No-op preconditions ────────────────────────────────────


class TestNoOps:
    @pytest.mark.asyncio
    async def test_empty_text_silent_noop(self):
        p = _make_pipeline()
        await speak_system_message(p, "")
        p._synthesize_and_send.assert_not_awaited()
        p._on_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_tts_silent_noop(self):
        """When TTS isn't initialised yet (boot race), skip
        without crashing."""
        p = _make_pipeline(has_tts=False)
        await speak_system_message(p, "alert!")
        p._synthesize_and_send.assert_not_awaited()
        p._on_event.assert_not_awaited()


# ─── Happy path ─────────────────────────────────────────────


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_synth_called_with_text(self):
        p = _make_pipeline()
        await speak_system_message(p, "hello world")
        p._synthesize_and_send.assert_awaited_once_with("hello world")

    @pytest.mark.asyncio
    async def test_emits_tts_end_when_we_started_the_utterance(self):
        """Bracket invariant: synth flipped _tts_started from
        False → True, so we own the close → emit tts_end + clear."""
        p = _make_pipeline(synth_starts_tts=True)
        await speak_system_message(p, "hi")
        # Pin: tts_end emitted with the accumulated tts_ms
        p._on_event.assert_awaited_once()
        ev = p._on_event.await_args.args[0]
        assert ev["type"] == "tts_end"
        assert ev["tts_ms"] == 250  # round(250)
        # Flag cleared so the next utterance starts fresh
        assert p._tts_started is False


# ─── Bracket invariant: prev_started = True (mid-utterance) ──


class TestMidUtteranceCall:
    @pytest.mark.asyncio
    async def test_does_NOT_emit_tts_end_when_already_started(self):
        """Pin: speak_system called mid-utterance must NOT emit
        tts_end — the active utterance's caller owns the close.
        Without this, double-tts_end would confuse Tab5's
        ring-buffer state machine."""
        p = _make_pipeline(initial_tts_started=True)  # mid-utt
        await speak_system_message(p, "interrupt!")
        # No tts_end emit (prev_started was True so we didn't
        # own the open).
        p._on_event.assert_not_awaited()
        # _tts_started preserved (caller still owns it)
        assert p._tts_started is True


# ─── Failure isolation ──────────────────────────────────────


class TestFailureIsolation:
    @pytest.mark.asyncio
    async def test_synth_exception_does_NOT_propagate(self):
        """Fire-and-forget invariant: a TTS failure during a
        speak_system call MUST NOT propagate up into the caller's
        coroutine.  Pin so a future refactor can't drop the
        try/except."""
        p = _make_pipeline(synth_raises=RuntimeError("piper crashed"))
        # Must NOT raise.
        await speak_system_message(p, "alert")

    @pytest.mark.asyncio
    async def test_synth_exception_still_runs_finally_emit_when_we_started(self):
        """Pin: even when synth raises, IF we started the
        utterance (somehow, mid-failure), the tts_end fires so
        Tab5's ring buffer flushes.  Pre-extract behaviour."""
        # Simulate: synth flips _tts_started THEN raises.
        async def _raises_after_starting(text):
            p._tts_started = True
            p._tts_total_ms = 100
            raise RuntimeError("crashed mid-way")

        p = _make_pipeline()
        p._synthesize_and_send = AsyncMock(side_effect=_raises_after_starting)

        await speak_system_message(p, "alert")

        # tts_end still fired (finally block ran)
        p._on_event.assert_awaited_once()
        assert p._tts_started is False

    @pytest.mark.asyncio
    async def test_tts_end_emit_failure_swallowed(self):
        """Best-effort emit: if `_on_event(tts_end)` raises
        (transport closed mid-stream), swallow at the close
        bracket — user's already heard the audio."""
        p = _make_pipeline()
        p._on_event = AsyncMock(side_effect=RuntimeError("ws closed"))

        # Must NOT raise.
        await speak_system_message(p, "alert")

        # Flag still cleared even though emit raised
        assert p._tts_started is False


# ─── tts_total_ms rounding ──────────────────────────────────


class TestTtsMsRounding:
    @pytest.mark.asyncio
    async def test_tts_ms_rounded_to_int(self):
        """Pin: tts_ms in the WS frame is `round(_tts_total_ms)` —
        Tab5 expects an int (the caption widget formats as
        `XX ms`)."""
        async def _synth(text):
            p._tts_started = True
            p._tts_total_ms = 247.6  # fractional ms

        p = _make_pipeline()
        p._synthesize_and_send = AsyncMock(side_effect=_synth)

        await speak_system_message(p, "x")
        ev = p._on_event.await_args.args[0]
        assert ev["tts_ms"] == 248  # rounded
        assert isinstance(ev["tts_ms"], int)
