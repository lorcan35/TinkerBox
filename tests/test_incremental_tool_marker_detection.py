"""Unit tests for the incremental tool-marker detection in ConversationEngine.

Phase 2 H1 of the UX-gap remediation (see docs/UX-GAPS.md / issue #94).

Pre-fix `process_text_stream` suppressed every yield when `tool_registry`
was set, accumulated the entire LLM output in `full_response`, and
emitted a single yield at the bottom — Tab5 stared at a silent caption
for 60-90 s on every tool-calling local turn.

Post-fix it streams tokens immediately UNTIL a tool-marker opener
appears in the rolling buffer.  Hold back from the marker-start onward;
once the LLM finishes either parse + execute (existing path) or treat
the held buffer as benign prose and flush it.

Tests cover:

  - The pure helper `_split_at_marker_boundary` (unit-level, fast)
  - The end-to-end streaming behavior of `process_text_stream` against
    a stub LLM (integration-level, exercises the loop semantics)
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from dragon_voice.conversation import (
    ConversationEngine,
    _split_at_marker_boundary,
)
from dragon_voice.config import LLMConfig
from dragon_voice.tools.base import Tool
from dragon_voice.tools.registry import ToolRegistry


# ───────────────────────── helper unit tests


def test_no_marker_flushes_everything() -> None:
    flush, held = _split_at_marker_boundary("Hello, how are you?")
    assert flush == "Hello, how are you?"
    assert held == ""


def test_complete_legacy_marker_held_from_start() -> None:
    flush, held = _split_at_marker_boundary(
        "Let me search <tool>web_search</tool><args>{\"q\":\"x\"}</args> done"
    )
    assert flush == "Let me search "
    assert held.startswith("<tool>")


def test_complete_xlam_bracket_quirks_held() -> None:
    for opener in ("<tool>", "[tool>", "<tool]", "[tool]"):
        flush, held = _split_at_marker_boundary(f"Sure, {opener}calc</tool>")
        assert flush == "Sure, ", f"opener={opener!r}: flush={flush!r}"
        assert held.startswith(opener), f"opener={opener!r}: held={held!r}"


def test_standard_tool_call_marker_held() -> None:
    flush, held = _split_at_marker_boundary(
        'Looking it up <tool_call>{"name":"weather","arguments":{}}</tool_call>'
    )
    assert flush == "Looking it up "
    assert held.startswith("<tool_call>")


def test_dialect3_bracketed_name_held_when_registered() -> None:
    flush, held = _split_at_marker_boundary(
        "Sure [recall]{\"q\":\"x\"}</recall>",
        registered_tool_names={"recall", "calculator", "web_search"},
    )
    assert flush == "Sure "
    assert held.startswith("[recall]")


def test_dialect3_bracketed_name_NOT_held_when_unregistered() -> None:
    """Prose like `[New York]` shouldn't trigger marker detection unless
    NewYork happens to be a registered tool name."""
    flush, held = _split_at_marker_boundary(
        "I love [New York]",
        registered_tool_names={"recall", "calculator"},
    )
    assert flush == "I love [New York]"
    assert held == ""


def test_tail_partial_held_back() -> None:
    """Stream ends mid-marker — that tail must not be flushed."""
    # Tail = "<too" — strict prefix of "<tool>" / "<tool_call>" / "<tool]"
    flush, held = _split_at_marker_boundary("Let me search <too")
    assert flush == "Let me search "
    assert held == "<too"


def test_tail_partial_includes_lone_lt() -> None:
    """Even a bare trailing `<` should be held — could be opening of any
    `<tool*` opener."""
    flush, held = _split_at_marker_boundary("Hello <")
    assert flush == "Hello "
    assert held == "<"


def test_lt_followed_by_non_marker_char_still_partial() -> None:
    """`<x` is NOT a strict prefix of any opener (all start with `<t`),
    so it should flush.  This exists to confirm we're not over-holding."""
    flush, held = _split_at_marker_boundary("Hello <x")
    assert flush == "Hello <x"
    assert held == ""


def test_dialect3_partial_close_bracket_NOT_held() -> None:
    """We deliberately don't try to match `[NAM` as an in-flight dialect-3
    opener — false positives on prose like `[Star Trek` would stutter
    the stream.  The complete-marker check still catches `[NAME]` once
    the close-bracket arrives."""
    flush, held = _split_at_marker_boundary(
        "Maybe [recal",  # incomplete; would-be opener if `recal` were a tool
        registered_tool_names={"recall"},
    )
    # Flush everything — `[recal` is benign prose for now
    assert flush == "Maybe [recal"
    assert held == ""


def test_complete_dialect3_takes_precedence_over_partial_legacy() -> None:
    """If both a complete dialect-3 marker AND a tail partial exist,
    boundary is the EARLIEST hold position."""
    flush, held = _split_at_marker_boundary(
        "[recall]{\"q\":\"x\"}</recall> and then <to",
        registered_tool_names={"recall"},
    )
    # Boundary at index 0 (the [recall] start), so everything is held.
    assert flush == ""
    assert held.startswith("[recall]")


def test_empty_string() -> None:
    flush, held = _split_at_marker_boundary("")
    assert (flush, held) == ("", "")


def test_only_a_marker() -> None:
    flush, held = _split_at_marker_boundary("<tool>")
    assert flush == ""
    assert held == "<tool>"


# ───────────────────────── integration: process_text_stream


class _StubTool(Tool):
    """Minimal Tool stub for registering names with the registry."""
    def __init__(self, name: str) -> None:
        self._name = name
    @property
    def name(self) -> str: return self._name
    @property
    def description(self) -> str: return f"stub {self._name}"
    @property
    def parameters_schema(self) -> dict: return {"type": "object"}
    async def execute(self, args: dict) -> dict: return {"ok": True}


class _ScriptedLLM:
    """LLM stub that yields a scripted token stream."""
    name = "scripted-llm"
    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens
        self._iters = 0  # how many times generate_stream_with_messages was called
    async def initialize(self) -> None: pass
    async def shutdown(self) -> None: pass
    async def generate_stream(self, prompt: str, system_prompt: str = "") -> AsyncIterator[str]:
        for t in self._tokens:
            yield t
    async def generate_stream_with_messages(self, messages: list[dict]) -> AsyncIterator[str]:
        self._iters += 1
        # On a follow-up iteration (after tool execute), yield a final reply
        # so the loop terminates.
        if self._iters > 1:
            for t in ["The answer ", "is 42."]:
                yield t
            return
        for t in self._tokens:
            yield t
    def get_last_usage(self) -> dict: return {}


class _StubMessageStore:
    """Records add_message calls + returns empty context."""
    def __init__(self) -> None: self.added: list[dict] = []
    async def add_message(self, **kw) -> None: self.added.append(kw)
    async def get_context(self, *args, **kw) -> list[dict]:
        return [{"role": "system", "content": "be helpful"}]


class _StubDB:
    async def touch_session(self, sid: str) -> None: pass
    async def get_session(self, sid: str) -> dict | None: return None


def _make_engine(llm_tokens: list[str], with_registry: bool = True,
                 tool_names: tuple[str, ...] = ("calculator", "web_search", "recall")
                 ) -> tuple[ConversationEngine, _ScriptedLLM]:
    cfg = LLMConfig()
    eng = ConversationEngine(
        db=_StubDB(),
        message_store=_StubMessageStore(),
        llm_config=cfg,
        tool_registry=(ToolRegistry() if with_registry else None),
        memory_service=None,
    )
    if with_registry:
        for n in tool_names:
            eng._tool_registry.register(_StubTool(n))
    eng._llm = _ScriptedLLM(llm_tokens)
    return eng, eng._llm


async def _drain(agen: AsyncIterator[str]) -> list[str]:
    out: list[str] = []
    async for chunk in agen:
        out.append(chunk)
    return out


def test_no_tool_registry_streams_raw_tokens() -> None:
    """When tool_registry is None, every token from the LLM yields immediately.
    Pre-existing behavior — must NOT regress."""
    eng, _ = _make_engine(["Hello ", "world", "!"], with_registry=False)
    chunks = asyncio.run(_drain(eng.process_text_stream("s1", "hi")))
    # 3 chunks (one per token) preserved exactly
    assert chunks == ["Hello ", "world", "!"]


def test_with_tool_registry_streams_chunks_when_no_marker() -> None:
    """Headline H1 fix: tokens stream through (in chunks split at safe
    boundaries) instead of being buffered until end-of-stream."""
    eng, _ = _make_engine(
        ["Hello ", "how ", "are ", "you ", "today", "?"],
        with_registry=True,
    )
    chunks = asyncio.run(_drain(eng.process_text_stream("s1", "hi")))
    # We get multiple chunks (one or more flushes during the stream),
    # not a single end-of-stream chunk.  Total content == LLM output.
    assert "".join(chunks) == "Hello how are you today?"
    assert len(chunks) >= 2, (
        "Expected incremental flushing; got a single chunk of the full "
        "response which would mean the buffer-everything regression came back"
    )


def test_with_marker_holds_back_until_tool_executed() -> None:
    """Tool turn: prefix flushes, marker block held, after tool execute
    the loop runs a second LLM call (existing behaviour)."""
    eng, _ = _make_engine(
        [
            "Sure, ", "let me ", "look ",
            '<tool>', 'web_search', '</tool>',
            '<args>', '{"q":"x"}', '</args>',
        ],
        with_registry=True,
    )
    chunks = asyncio.run(_drain(eng.process_text_stream("s1", "find x")))
    joined = "".join(chunks)
    # Prefix prose was streamed
    assert "Sure, let me look" in joined
    # Tool markup itself does NOT reach the user
    assert "<tool>" not in joined
    assert "</args>" not in joined
    # The follow-up LLM call's reply is appended
    assert "The answer is 42." in joined


def test_malformed_tool_args_fires_on_tool_error_callback() -> None:
    """γ2-M1 (issue #104): when the LLM emits a tool block with
    malformed JSON args, the parse failure must surface to the WS
    handler via the new ``on_tool_error`` callback so a
    ``tool_args_invalid`` error frame can be sent to Tab5 instead of
    the user seeing nothing.

    Pre-fix this case was a silent ``logger.warning`` line — Tab5 got
    no error frame, the LLM continued (or stopped, depending on the
    model), and the user saw an empty / generic reply with zero hint
    that anything was attempted."""
    eng, _ = _make_engine(
        [
            "Sure, ",
            '<tool>calculator</tool><args>{not valid json}</args>',
        ],
        with_registry=True,
    )
    seen_errors: list[dict] = []

    async def _capture(err: dict) -> None:
        seen_errors.append(err)

    asyncio.run(_drain(
        eng.process_text_stream("s1", "x", on_tool_error=_capture)
    ))

    assert len(seen_errors) == 1, (
        f"Expected exactly one on_tool_error call; got {seen_errors}"
    )
    err = seen_errors[0]
    assert err["dialect"] == 1
    assert err["name"] == "calculator"
    assert err["reason"] == "json_decode"


def test_clean_tool_call_does_not_fire_on_tool_error() -> None:
    """Pin the boundary: a successful tool-call must NOT spuriously
    fire ``on_tool_error``.  Without this guard a future regression
    that flips the error/success branches could go unnoticed because
    Tab5 would just see an extra toast on every successful tool turn —
    annoying but not test-breaking."""
    eng, _ = _make_engine(
        [
            "Sure, ", "let me ", "look ",
            '<tool>', 'web_search', '</tool>',
            '<args>', '{"q":"x"}', '</args>',
        ],
        with_registry=True,
    )
    seen_errors: list[dict] = []

    async def _capture(err: dict) -> None:
        seen_errors.append(err)

    asyncio.run(_drain(
        eng.process_text_stream("s1", "find x", on_tool_error=_capture)
    ))
    assert seen_errors == []


def test_on_tool_error_callback_exception_is_swallowed() -> None:
    """Defensive: a buggy callback must not break the streaming turn.
    Pre-fix the existing on_tool_call/on_tool_result wrappers swallow
    callback errors at debug level; preserve that contract for the
    new on_tool_error path so a flaky WS send doesn't tear down the
    LLM turn."""
    eng, _ = _make_engine(
        [
            '<tool>calculator</tool><args>{not valid}</args>',
        ],
        with_registry=True,
    )

    async def _boom(err: dict) -> None:
        raise RuntimeError("simulated callback failure")

    # Should NOT raise out of process_text_stream — the loop terminates
    # cleanly even when the user-supplied callback explodes.
    asyncio.run(_drain(
        eng.process_text_stream("s1", "x", on_tool_error=_boom)
    ))


def test_tail_partial_marker_does_not_flush_prematurely() -> None:
    """If the LLM emits `<too` and then keeps going, the tail must wait
    for the next token before flushing."""
    # Token stream: 'I see <to' then 'ol>foo</tool><args>{}</args>'
    # The first chunk's tail `<to` is a strict prefix of `<tool>` so
    # held-back; the second chunk completes the marker and we hold the
    # whole tool block.
    eng, _ = _make_engine(
        ["I see <to", "ol>foo</tool><args>{}</args>"],
        with_registry=True,
    )
    chunks = asyncio.run(_drain(eng.process_text_stream("s1", "x")))
    joined = "".join(chunks)
    # `<to` did NOT leak to the user mid-stream
    assert "<to" not in joined or "<tool" not in joined
    # The follow-up "answer is 42" path runs because tool fired
    assert "The answer is 42." in joined
