"""Regression test for audit A2 + A3 (voice path tool plumbing).

Pre-fix:
  * A2: pipeline._process_utterance called process_text_stream() WITHOUT
    on_tool_* kwargs, so Tab5 never saw tool_call/tool_result frames on
    voice turns even though the same prompt typed into chat showed them.
  * A3: end-of-stream guard hard-coded the generic apology even when
    tools had fired.  Voice "remember magenta" with an FC-style empty
    NL reply would say "Sorry, I couldn't generate a response..."
    instead of "Got it -- magenta."

Strategy: drive `_process_utterance` with mocked STT/TTS/conversation
engine.  The fake conversation engine simulates an FC-style turn:
fires `on_tool_call` + `on_tool_result` for the `remember` tool, then
yields zero usable text tokens.  We assert:

  1. The pipeline forwarded the tool_call to the user-supplied
     on_tool_call callback (A2).
  2. self._tool_calls_this_turn was populated by the wrapper (A3 prep).
  3. The end-of-stream emit went through `on_event` carrying the
     synthesise_wrap text ("Got it -- magenta."), NOT the legacy
     "Sorry, I couldn't generate..." apology.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from dragon_voice.config import VoiceConfig
from dragon_voice.pipeline import VoicePipeline


class _FakeSTT:
    sample_rate = 16000

    async def transcribe(self, audio: bytes, sr: int) -> str:
        return "remember magenta is my favorite color"


class _FakeTTS:
    sample_rate = 22050
    name = "fake-tts"

    async def synthesize(self, text: str) -> bytes:
        return b""

    def kill_active_procs(self) -> None:
        pass


class _FakeConversationEngine:
    """Simulates an FC-style turn: fires on_tool_call + on_tool_result for
    `remember`, then yields zero useful text."""

    def __init__(self) -> None:
        self.calls_seen: list[dict] = []

    def process_text_stream(
        self,
        *,
        session_id: str,
        text: str,
        input_mode: str,
        audio_duration_s: float = 0.0,
        on_tool_call=None,
        on_tool_result=None,
        on_tool_error=None,
    ) -> AsyncIterator[str]:
        async def _gen() -> AsyncIterator[str]:
            self.calls_seen.append({"session": session_id, "text": text})
            if on_tool_call is not None:
                await on_tool_call({
                    "tool": "remember",
                    "args": {"fact": "magenta is my favorite color"},
                })
            if on_tool_result is not None:
                await on_tool_result({
                    "tool": "remember",
                    "result": {"stored": "magenta is my favorite color"},
                    "execution_ms": 3,
                })
            # FC-style empty NL: yield a single stray bracket that the
            # text path's looks_like_useful_text recognises as junk.
            yield "<"

        return _gen()


@pytest.fixture
def voice_pipeline_with_mocks():
    cfg = VoiceConfig()
    cfg.llm.backend = "ollama"  # not 'tinkerclaw' so the ConvEngine branch runs

    events: list[dict] = []
    audio_chunks: list[bytes] = []
    tool_calls_observed: list[dict] = []
    tool_results_observed: list[dict] = []

    async def on_event(e: dict) -> None:
        events.append(e)

    async def on_audio(b: bytes) -> None:
        audio_chunks.append(b)

    async def on_tool_call(c: dict) -> None:
        tool_calls_observed.append(c)

    async def on_tool_result(r: dict) -> None:
        tool_results_observed.append(r)

    fake_conv = _FakeConversationEngine()

    pipeline = VoicePipeline(
        cfg,
        on_audio=on_audio,
        on_event=on_event,
        conversation_engine=fake_conv,
        session_id="test-session-1",
        on_tool_call=on_tool_call,
        on_tool_result=on_tool_result,
    )
    pipeline._stt = _FakeSTT()  # type: ignore[assignment]
    pipeline._tts = _FakeTTS()  # type: ignore[assignment]

    return {
        "pipeline": pipeline,
        "events": events,
        "tool_calls": tool_calls_observed,
        "tool_results": tool_results_observed,
        "fake_conv": fake_conv,
    }


def test_voice_path_forwards_tool_callbacks_to_user(voice_pipeline_with_mocks) -> None:
    """A2: tool_call/tool_result callbacks must reach the user-supplied
    callbacks the server passed in."""
    p = voice_pipeline_with_mocks["pipeline"]
    asyncio.run(p._process_utterance(b"\x00\x00" * 1600))

    tool_calls = voice_pipeline_with_mocks["tool_calls"]
    tool_results = voice_pipeline_with_mocks["tool_results"]
    assert len(tool_calls) == 1, f"expected 1 tool_call forwarded, got {tool_calls}"
    assert tool_calls[0]["tool"] == "remember"
    assert len(tool_results) == 1, f"expected 1 tool_result forwarded, got {tool_results}"
    assert tool_results[0]["tool"] == "remember"


def test_voice_path_populates_per_turn_tracker(voice_pipeline_with_mocks) -> None:
    """A3 prep: the wrapped callbacks must populate self._tool_calls_this_turn
    so the empty-reply guard has something to wrap."""
    p = voice_pipeline_with_mocks["pipeline"]
    asyncio.run(p._process_utterance(b"\x00\x00" * 1600))

    tracker = p._tool_calls_this_turn
    assert len(tracker) == 1, f"expected 1 tracker entry, got {tracker}"
    assert tracker[0]["tool"] == "remember"
    assert tracker[0]["args"] == {"fact": "magenta is my favorite color"}
    # Result merged into the same record (not appended as a duplicate)
    assert tracker[0].get("result") == {"stored": "magenta is my favorite color"}


def test_voice_path_emits_per_tool_wrap_not_generic_apology(voice_pipeline_with_mocks) -> None:
    """A3 + C5: when the LLM stream produces only bracket-noise AND a
    tool fired, the end-of-stream guard must emit the synthesise_wrap
    template ("Got it -- magenta...") not the legacy generic apology."""
    p = voice_pipeline_with_mocks["pipeline"]
    asyncio.run(p._process_utterance(b"\x00\x00" * 1600))

    events = voice_pipeline_with_mocks["events"]
    llm_emits = [e["text"] for e in events if e.get("type") == "llm" and "text" in e]
    full_text = " ".join(llm_emits)
    # Per-tool wrap should appear ...
    assert "Got it" in full_text, f"expected per-tool wrap in emits, got {llm_emits!r}"
    assert "magenta" in full_text, f"expected fact in wrap, got {llm_emits!r}"
    # ... and the legacy apology should NOT.
    assert "Sorry, I couldn't generate" not in full_text, (
        f"legacy apology leaked despite tool fires: {llm_emits!r}"
    )


def test_voice_path_falls_through_to_apology_when_no_tools_fire(
    voice_pipeline_with_mocks,
) -> None:
    """W15-H09 fallback still applies when zero tools fired -- pipeline
    should not silently drop a hung LLM stream."""
    info = voice_pipeline_with_mocks
    p = info["pipeline"]

    # Replace conv engine with one that fires NO tool callbacks and
    # yields only bracket noise.
    class _NoToolConv:
        def process_text_stream(self, *, session_id, text, input_mode,
                                audio_duration_s=0.0,
                                on_tool_call=None, on_tool_result=None,
                                on_tool_error=None):
            async def _gen():
                yield "<"
            return _gen()

    p._conversation_engine = _NoToolConv()
    asyncio.run(p._process_utterance(b"\x00\x00" * 1600))

    events = info["events"]
    llm_emits = [e["text"] for e in events if e.get("type") == "llm" and "text" in e]
    full_text = " ".join(llm_emits)
    assert "Sorry, I couldn't generate" in full_text, (
        f"expected legacy fallback when no tools fired, got {llm_emits!r}"
    )


def test_voice_path_skips_wrap_when_useful_text_present(voice_pipeline_with_mocks) -> None:
    """Happy path: real LLM text must NOT be overwritten by a wrap."""
    info = voice_pipeline_with_mocks
    p = info["pipeline"]

    class _RealReplyConv:
        def process_text_stream(self, *, session_id, text, input_mode,
                                audio_duration_s=0.0,
                                on_tool_call=None, on_tool_result=None,
                                on_tool_error=None):
            async def _gen():
                if on_tool_call is not None:
                    await on_tool_call({"tool": "remember", "args": {"fact": "x"}})
                if on_tool_result is not None:
                    await on_tool_result({"tool": "remember", "result": {"stored": "x"}})
                # Real natural-language reply
                yield "Sure thing, "
                yield "I'll remember that."
            return _gen()

    p._conversation_engine = _RealReplyConv()
    asyncio.run(p._process_utterance(b"\x00\x00" * 1600))

    events = info["events"]
    llm_emits = [e["text"] for e in events if e.get("type") == "llm" and "text" in e]
    full_text = " ".join(llm_emits)
    assert "Sure thing" in full_text, f"real reply was clobbered: {llm_emits!r}"
    assert "I'll remember that." in full_text
    assert "Got it" not in full_text, f"wrap fired despite real reply: {llm_emits!r}"
    assert "Sorry, I couldn't generate" not in full_text
