"""Tests for B4 (#146): MAX_TOOL_CALLS limit reached fires a
structured `on_tool_error` callback instead of silently stripping.

Pre-fix: when an LLM emitted a 4th tool call after MAX_TOOL_CALLS=3
was hit, the conversation engine fell through to the strip-markup
branch and the user saw a reply that read as if it had been cut off
mid-thought ("Let me check the calendar… <silence>").

Post-fix: the engine fires `on_tool_error` with code
`tool_call_limit_reached` so Tab5 (via the WS handler's wrapped
callback) renders a transient toast and the user knows why the
chain stopped.

Strategy: fake the LLM + tool registry, drive `process_text_stream`
through 4 tool-call rounds, observe that the 4th iteration emits
the limit-reached error.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest

from dragon_voice import conversation as convo_mod
from dragon_voice.conversation import ConversationEngine


class _FakeLLM:
    """Echoes back a `<tool>echo</tool><args>{}</args>` token sequence
    every call — forces the engine to recognise a tool call on every
    iteration."""

    name = "fake-llm"

    def __init__(self) -> None:
        self.calls = 0
        self._llm_config = type("C", (), {"backend": "ollama"})()

    async def generate_stream_with_messages(
        self, messages: list[dict]
    ) -> AsyncIterator[str]:
        self.calls += 1
        for tok in "<tool>echo</tool><args>{}</args>":
            yield tok


class _FakeRegistry:
    """Always reports a tool call present, executes synchronously."""

    def __init__(self) -> None:
        self._tools = {"echo": object()}

    def has_tool_call(self, text: str) -> bool:
        return "<tool>echo</tool>" in text

    def parse_tool_calls_with_errors(self, text: str):
        return [{"tool": "echo", "args": {}}], []

    async def execute(self, name: str, args: dict[str, Any]) -> dict:
        return {"tool": name, "result": "ack", "execution_ms": 1}


class _FakeMessageStore:
    """Minimal stand-in: records add_message + returns a context with
    a system message so the engine doesn't trip on empty context."""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def add_message(self, **kw) -> None:
        self.messages.append(kw)

    async def get_context(self, session_id: str, max_messages: int = 10) -> list[dict]:
        return [{"role": "system", "content": "you are a fake assistant"}]


class _FakeDB:
    async def touch_session(self, sid: str) -> None:
        return None


def _make_engine() -> tuple[ConversationEngine, _FakeLLM, list[dict]]:
    llm = _FakeLLM()
    eng = ConversationEngine.__new__(ConversationEngine)
    eng._llm = llm
    eng._tool_registry = _FakeRegistry()
    eng._messages = _FakeMessageStore()
    eng._db = _FakeDB()
    eng._memory_service = None
    eng._llm_config = llm._llm_config

    async def _build_context(session_id, user_text):
        return [{"role": "system", "content": "ctx"}]

    eng._build_context = _build_context  # type: ignore[assignment]
    return eng, llm, []


def test_max_tool_calls_emits_tool_call_limit_error() -> None:
    eng, llm, _ = _make_engine()
    errors: list[dict] = []
    calls: list[dict] = []
    results: list[dict] = []

    async def on_tool_call(c):
        calls.append(c)

    async def on_tool_result(r):
        results.append(r)

    async def on_tool_error(e):
        errors.append(e)

    async def go():
        out = []
        async for tok in eng.process_text_stream(
            session_id="s1", text="please loop tools",
            on_tool_call=on_tool_call,
            on_tool_result=on_tool_result,
            on_tool_error=on_tool_error,
        ):
            out.append(tok)
        return out

    asyncio.run(go())

    # MAX_TOOL_CALLS = 3 → engine fires the tool 3 times, then on the
    # 4th iteration sees has_tool_call but tool_calls_made=3, takes
    # the new B4 branch and fires the limit error.
    assert len(calls) == convo_mod.MAX_TOOL_CALLS, (
        f"expected {convo_mod.MAX_TOOL_CALLS} tool calls, got {len(calls)}"
    )
    assert len(errors) == 1, f"expected 1 limit error, got {errors!r}"
    err = errors[0]
    assert err.get("code") == "tool_call_limit_reached"
    assert err.get("limit") == convo_mod.MAX_TOOL_CALLS
    # Message must mention the limit so Tab5's toast is actionable.
    assert str(convo_mod.MAX_TOOL_CALLS) in err.get("message", "")


def test_no_limit_error_when_under_threshold() -> None:
    """Regression guard: when the LLM stops calling tools before the
    limit, the limit-error must NOT fire."""

    class _StopAfterOne:
        name = "stop-after-one"

        def __init__(self) -> None:
            self.calls = 0
            self._llm_config = type("C", (), {"backend": "ollama"})()

        async def generate_stream_with_messages(self, messages):
            self.calls += 1
            if self.calls == 1:
                for tok in "<tool>echo</tool><args>{}</args>":
                    yield tok
            else:
                for tok in "Done.":
                    yield tok

    eng, _, _ = _make_engine()
    eng._llm = _StopAfterOne()
    errors: list[dict] = []

    async def on_tool_error(e):
        errors.append(e)

    async def go():
        out = []
        async for tok in eng.process_text_stream(
            session_id="s2", text="x",
            on_tool_error=on_tool_error,
        ):
            out.append(tok)
        return out

    asyncio.run(go())

    assert errors == [], f"unexpected limit error fired: {errors!r}"


def test_limit_error_omitted_when_no_callback_provided() -> None:
    """Should not raise when on_tool_error is None — limit detection
    must be defensive."""
    eng, _, _ = _make_engine()

    async def go():
        out = []
        async for tok in eng.process_text_stream(
            session_id="s3", text="x",
            # No callbacks at all
        ):
            out.append(tok)
        return out

    # Just must not raise.
    asyncio.run(go())
