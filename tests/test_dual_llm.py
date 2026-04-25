"""Unit tests for the DualModelBackend pipeline orchestration.

Mocks the two sub-backends so each branch of the dual logic can be
exercised without spinning up a real Ollama server.

Branches under test:
  1. tool-result in context → responder only (picker skipped)
  2. picker emits tool marker → yield verbatim, responder skipped
  3. picker emits useful chat text → yield it, responder skipped
  4. picker emits junk/empty → fall through to responder
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from dragon_voice.config import LLMConfig
from dragon_voice.llm.base import LLMBackend
from dragon_voice.llm.dual import DualModelBackend


class _FakeBackend(LLMBackend):
    """Records what it was called with and yields a scripted token stream."""

    def __init__(self, label: str, tokens: list[str]) -> None:
        self._label = label
        self._tokens = tokens
        self.calls: list[list[dict]] = []
        self.initialized = False
        self.shutdown_called = False

    @property
    def name(self) -> str:
        return self._label

    async def initialize(self) -> None:
        self.initialized = True

    async def shutdown(self) -> None:
        self.shutdown_called = True

    async def generate_stream(
        self, prompt: str, system_prompt: str = ""
    ) -> AsyncIterator[str]:
        # Record + replay
        self.calls.append([{"role": "user", "content": prompt}])
        for t in self._tokens:
            yield t

    async def generate_stream_with_messages(
        self, messages: list[dict]
    ) -> AsyncIterator[str]:
        self.calls.append(list(messages))
        for t in self._tokens:
            yield t


def _make_dual(picker: _FakeBackend, responder: _FakeBackend) -> DualModelBackend:
    """Construct a DualModelBackend with the create_llm factory monkeypatched
    to return our fakes in (picker, responder) order."""
    cfg = LLMConfig()
    cfg.dual_picker_backend = "ollama"
    cfg.dual_picker_model = "fake-picker"
    cfg.dual_responder_backend = "ollama"
    cfg.dual_responder_model = "fake-responder"

    pending = [picker, responder]

    import dragon_voice.llm as llm_pkg

    real_create = llm_pkg.create_llm

    def fake_create(_subcfg):
        return pending.pop(0)

    llm_pkg.create_llm = fake_create
    try:
        dual = DualModelBackend(cfg)
    finally:
        llm_pkg.create_llm = real_create
    return dual


async def _drain(agen) -> str:
    chunks: list[str] = []
    async for c in agen:
        chunks.append(c)
    return "".join(chunks)


def test_responder_only_when_last_message_is_tool() -> None:
    picker = _FakeBackend("picker", ["should not run"])
    responder = _FakeBackend("responder", ["Got it ", "— ", "your color is magenta."])
    dual = _make_dual(picker, responder)

    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "What is my favorite color?"},
        {"role": "assistant", "content": "<tool>recall</tool><args>{}</args>"},
        {"role": "tool", "content": '<tool_result>{"color":"magenta"}</tool_result>'},
    ]
    out = asyncio.run(_drain(dual.generate_stream_with_messages(msgs)))

    assert out == "Got it — your color is magenta."
    assert picker.calls == []  # picker skipped on responder phase
    assert responder.calls == [msgs]


def test_picker_tool_marker_yielded_verbatim() -> None:
    # The picker fires a tool — dual must yield the markup so
    # ConversationEngine's parser sees it and executes the tool.
    tool_chunks = ["<tool>", "calculator", "</tool>", "<args>", '{"x":1}', "</args>"]
    picker = _FakeBackend("picker", tool_chunks)
    responder = _FakeBackend("responder", ["should not run"])
    dual = _make_dual(picker, responder)

    msgs = [{"role": "user", "content": "what's 1+1"}]
    out = asyncio.run(_drain(dual.generate_stream_with_messages(msgs)))

    assert out == "".join(tool_chunks)
    assert len(picker.calls) == 1
    assert responder.calls == []  # responder NOT run when picker fired a tool


def test_picker_useful_chat_text_returned_directly() -> None:
    # Picker emits a complete chat reply, no tool — cheap chat path.
    picker = _FakeBackend(
        "picker",
        ["Sure, ", "which one would ", "you like to drink?"],
    )
    responder = _FakeBackend("responder", ["should not run"])
    dual = _make_dual(picker, responder)

    msgs = [{"role": "user", "content": "ask me to choose"}]
    out = asyncio.run(_drain(dual.generate_stream_with_messages(msgs)))

    assert out == "Sure, which one would you like to drink?"
    assert responder.calls == []


def test_picker_junk_falls_through_to_responder() -> None:
    # Picker emits residual XML / bracket noise that fails the
    # useful-text gate.  Dual must call the responder.
    picker = _FakeBackend("picker", ["[recall]", "</recall>"])
    responder = _FakeBackend(
        "responder", ["Your favorite color is magenta."],
    )
    dual = _make_dual(picker, responder)

    msgs = [{"role": "user", "content": "what's my color"}]
    out = asyncio.run(_drain(dual.generate_stream_with_messages(msgs)))

    assert out == "Your favorite color is magenta."
    assert len(picker.calls) == 1
    assert len(responder.calls) == 1


def test_picker_empty_falls_through_to_responder() -> None:
    # Picker emits literally nothing — responder still runs.
    picker = _FakeBackend("picker", [])
    responder = _FakeBackend("responder", ["Hello there!"])
    dual = _make_dual(picker, responder)

    msgs = [{"role": "user", "content": "hi"}]
    out = asyncio.run(_drain(dual.generate_stream_with_messages(msgs)))

    assert out == "Hello there!"
    assert len(picker.calls) == 1
    assert len(responder.calls) == 1


def test_initialize_and_shutdown_propagate_to_both_subs() -> None:
    picker = _FakeBackend("picker", [])
    responder = _FakeBackend("responder", [])
    dual = _make_dual(picker, responder)

    asyncio.run(dual.initialize())
    assert picker.initialized and responder.initialized

    asyncio.run(dual.shutdown())
    assert picker.shutdown_called and responder.shutdown_called


def test_shutdown_runs_both_even_if_first_raises() -> None:
    class _FlakyBackend(_FakeBackend):
        async def shutdown(self) -> None:
            self.shutdown_called = True
            raise RuntimeError("boom")

    picker = _FlakyBackend("picker", [])
    responder = _FakeBackend("responder", [])
    dual = _make_dual(picker, responder)

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(dual.shutdown())
    # Responder shutdown still ran despite the picker raising.
    assert responder.shutdown_called
