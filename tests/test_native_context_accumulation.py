"""Native tool-calling path: prompt-prefix + history discipline.

The fast native path (llm.native_tools + a SupportsNativeTools backend)
must, for each turn:

  1. Build the prompt prefix EXACTLY ONCE — the static prefix (system +
     guidance + few-shot + tool schemas) stays byte-identical across loop
     iterations so llama-server's prompt cache can reuse it.
  2. Cap the prior-turn history window to NATIVE_HISTORY_MSGS so stale fat
     tool-result blobs don't dominate CPU prefill on the 1B model.
  3. Accumulate the CURRENT turn's tool round-trip IN-MEMORY as proper
     OpenAI `tool_calls` / `tool` messages — not by re-fetching the
     fattened history from the DB — so a multi-tool chain keeps its own
     results regardless of the history cap.

These are the latency levers landed after the dotprod rebuild: the
6431-token prompt was dominated by re-fetched history, and re-building the
prefix every iteration busted the prompt cache.
"""
from __future__ import annotations

import asyncio
from typing import Any

from dragon_voice import conversation as convo_mod
from dragon_voice.conversation import ConversationEngine


class _NativeLLM:
    """Implements the SupportsNativeTools contract. Returns a tool call for
    the first `tool_rounds` invocations, then a final text answer. Records
    the messages passed on every call so the test can inspect accumulation."""

    name = "native-fake"

    def __init__(self, tool_rounds: int = 2) -> None:
        self.tool_rounds = tool_rounds
        self.calls = 0
        self.seen: list[list[dict]] = []

    async def generate_with_tools(self, messages, tools, **kw) -> dict:
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        if self.calls <= self.tool_rounds:
            return {"tool_calls": [{"name": "echo", "args": {"n": self.calls}}],
                    "content": ""}
        return {"tool_calls": [], "content": "all done"}


class _Registry:
    def __init__(self) -> None:
        self._tools = {"echo": object()}

    def openai_tools(self) -> list[dict]:
        return [{"type": "function",
                 "function": {"name": "echo", "description": "echo",
                              "parameters": {"type": "object", "properties": {}}}}]

    def get(self, name: str):
        return self._tools.get(name)

    async def execute(self, name: str, args: dict[str, Any]) -> dict:
        return {"tool": name, "result": {"echoed": args}, "execution_ms": 1}


class _MsgStore:
    def __init__(self) -> None:
        self.added: list[dict] = []

    async def add_message(self, **kw) -> None:
        self.added.append(kw)


class _DB:
    async def touch_session(self, sid: str) -> None:
        return None


def _engine(llm: _NativeLLM) -> ConversationEngine:
    eng = ConversationEngine.__new__(ConversationEngine)
    eng._llm = llm
    eng._tool_registry = _Registry()
    eng._messages = _MsgStore()
    eng._db = _DB()
    eng._memory_service = None
    eng._media_store = None
    eng._llm_config = type(
        "C", (), {"backend": "lmstudio", "native_tools": True}
    )()
    return eng


def _run_native(eng: ConversationEngine, text: str = "what's on my calendar"):
    build_calls = {"n": 0, "max_msgs": "unset"}

    async def fake_build_context(session_id, user_text, inject_tool_prose=True,
                                 light_memory=False, max_msgs=None,
                                 inject_memory=True, drop_tool_history=False):
        build_calls["n"] += 1
        build_calls["max_msgs"] = max_msgs
        return [{"role": "system", "content": "sys"},
                {"role": "user", "content": user_text}]

    eng._build_context = fake_build_context  # type: ignore[assignment]

    async def go():
        out: list[str] = []
        async for tok in eng._process_text_stream_native(
            "s1", text, "text", None, None, None, None, None,
        ):
            out.append(tok)
        return out

    return asyncio.run(go()), build_calls


def test_compact_tool_result_caps_fat_list() -> None:
    # Simulate gmail_unread returning 10 emails with long snippets + links.
    big = {
        "count": 10,
        "messages": [
            {"from": f"sender{i}@x.com", "subject": f"Subject {i}",
             "snippet": "x" * 500, "htmlLink": "https://mail.google.com/" + "y" * 80,
             "id": f"id{i}"}
            for i in range(10)
        ],
    }
    out = convo_mod._compact_tool_result(big)
    # Stays within the prefill budget instead of ~6KB of raw JSON.
    assert len(out) <= convo_mod._TOOL_RESULT_CHAR_BUDGET
    # The summary-critical count + senders survive; the list is trimmed.
    assert '"count": 10' in out
    assert "more)" in out
    assert "sender0@x.com" in out
    # Voice-useless fields are dropped entirely, not just truncated.
    assert "x" * 200 not in out  # snippet dropped
    assert "https://" not in out  # htmlLink dropped


def test_compact_tool_result_passthrough_small() -> None:
    small = {"ok": True, "result": "done"}
    out = convo_mod._compact_tool_result(small)
    assert '"ok": true' in out
    assert "done" in out


def test_build_context_drops_prior_tool_history() -> None:
    # _build_context(drop_tool_history=True) must strip prior turns' tool +
    # <tool> marker messages from the prefill history (cache-stability), while
    # keeping the system prompt + real user/assistant text.
    eng = ConversationEngine.__new__(ConversationEngine)
    eng._memory_service = None
    eng._tool_registry = None
    eng._media_store = None
    eng._llm_config = type("C", (), {"backend": "lmstudio"})()

    class _MS:
        async def get_context(self, session_id, max_messages=10, media_store=None):
            return [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "whats on my calendar"},
                {"role": "assistant", "content": "<tool>calendar_today</tool><args>{}</args>"},
                {"role": "tool", "content": "<tool_result>{...}</tool_result>"},
                {"role": "assistant", "content": "You have 3 events"},
                {"role": "user", "content": "any unread email?"},
            ]

    eng._messages = _MS()
    ctx = asyncio.run(
        eng._build_context("s1", "any unread email?", inject_tool_prose=False,
                           inject_memory=False, drop_tool_history=True)
    )
    roles = [m["role"] for m in ctx]
    assert "tool" not in roles
    assert not any(
        m["role"] == "assistant" and m["content"].lstrip().startswith("<tool>")
        for m in ctx
    )
    # Real user + assistant-answer turns survive.
    contents = [m["content"] for m in ctx]
    assert "You have 3 events" in contents
    assert "whats on my calendar" in contents


def test_native_builds_prefix_once_with_capped_history() -> None:
    llm = _NativeLLM(tool_rounds=2)
    eng = _engine(llm)
    out, build_calls = _run_native(eng)
    # Prefix built exactly once despite two tool rounds + a final synth call.
    assert build_calls["n"] == 1, f"expected 1 build, got {build_calls['n']}"
    assert build_calls["max_msgs"] == convo_mod.NATIVE_HISTORY_MSGS
    assert "".join(out) == "all done"


def test_native_accumulates_tool_roundtrip_in_memory() -> None:
    llm = _NativeLLM(tool_rounds=2)
    eng = _engine(llm)
    _run_native(eng)
    # Three LLM calls: round 1, round 2, final synthesis.
    assert llm.calls == 3, f"expected 3 LLM calls, got {llm.calls}"
    n0, n1, n2 = (len(s) for s in llm.seen)
    # Each tool round appends exactly two messages: the assistant tool_calls
    # turn and the tool result. The history cap must NOT truncate them.
    assert n1 - n0 == 2, f"round 1 should append 2 msgs, got {n1 - n0}"
    assert n2 - n1 == 2, f"round 2 should append 2 msgs, got {n2 - n1}"
    # The appended pair is proper OpenAI shape, correlated by tool_call_id.
    pair = llm.seen[2][n1:]
    assert pair[0]["role"] == "assistant" and pair[0]["tool_calls"]
    assert pair[1]["role"] == "tool" and "tool_call_id" in pair[1]
    assert pair[0]["tool_calls"][0]["id"] == pair[1]["tool_call_id"]
    # The tool message carries the executed result, not the legacy XML text.
    assert "echoed" in pair[1]["content"]
