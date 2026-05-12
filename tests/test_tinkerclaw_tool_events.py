"""W7-A: gateway tool-event surfacing from TinkerClawBackend SSE.

Wave 7-A of the 2026-05-11 cross-stack audit.  Mode 3 routes LLM
inference to the TinkerClaw agent gateway via OpenAI-compatible SSE
on `/v1/chat/completions`.  Pre-W7-A the parser silently skipped
`delta.tool_calls` entries (only `delta.content` was read), so users
never saw the gateway's actual agentic activity in the Tab5
agent_log feed.

This module verifies:
  * complete tool calls fire the registered on_tool_call callback
    with Dragon's standard payload shape: {"tool": ..., "args": ...}
  * incremental name + arguments deltas are reassembled correctly
  * multiple parallel tool calls (different `index` values) all surface
  * malformed JSON arguments fall back to {"_raw": "..."} rather than
    raising
  * callbacks that raise are swallowed (a buggy handler must NOT tear
    down the LLM stream)
  * if no handler is registered, the parser is a no-op (no regression
    on existing behaviour)

Run:
    python3 -m pytest tests/test_tinkerclaw_tool_events.py -v
"""

from __future__ import annotations

import asyncio
import json
import unittest

# Construct the buffer + flush helpers in isolation — they're static/
# instance methods on TinkerClawBackend but don't need a live HTTP
# session.  Tests subclass-spy the callback instead of standing up
# a real ConnState.
from dragon_voice.llm.tinkerclaw_llm import TinkerClawBackend


class _NoInitBackend(TinkerClawBackend):
    """Skip the real __init__ — we only exercise the helpers."""

    def __init__(self):  # type: ignore[override]
        self._on_tool_call = None


def _delta(index: int, *, id_: str = "", name: str = "", args: str = "") -> dict:
    """Build one OpenAI tool_calls delta fragment."""
    out: dict = {"index": index}
    if id_:
        out["id"] = id_
    fn: dict = {}
    if name:
        fn["name"] = name
    if args:
        fn["arguments"] = args
    if fn:
        out["function"] = fn
    return out


class TestAccumulateToolCallDeltas(unittest.TestCase):
    def test_empty_or_none_is_noop(self):
        buf: dict = {}
        TinkerClawBackend._accumulate_tool_call_deltas(None, buf)
        TinkerClawBackend._accumulate_tool_call_deltas([], buf)
        self.assertEqual(buf, {})

    def test_single_call_assembled_across_fragments(self):
        buf: dict = {}
        # OpenAI streams: id arrives first, then name, then args char-by-char
        TinkerClawBackend._accumulate_tool_call_deltas(
            [_delta(0, id_="call_x")], buf,
        )
        TinkerClawBackend._accumulate_tool_call_deltas(
            [_delta(0, name="web_search")], buf,
        )
        for piece in ['{"query":', ' "weather"', "}"]:
            TinkerClawBackend._accumulate_tool_call_deltas(
                [_delta(0, args=piece)], buf,
            )
        self.assertEqual(buf[0]["id"], "call_x")
        self.assertEqual(buf[0]["name"], "web_search")
        self.assertEqual(buf[0]["arguments"], '{"query": "weather"}')

    def test_multiple_parallel_calls(self):
        buf: dict = {}
        TinkerClawBackend._accumulate_tool_call_deltas(
            [_delta(0, name="search"), _delta(1, name="remember")], buf,
        )
        TinkerClawBackend._accumulate_tool_call_deltas(
            [_delta(0, args='{"q":"x"}'), _delta(1, args='{"f":"y"}')], buf,
        )
        self.assertEqual(buf[0]["name"], "search")
        self.assertEqual(buf[1]["name"], "remember")
        self.assertEqual(buf[0]["arguments"], '{"q":"x"}')
        self.assertEqual(buf[1]["arguments"], '{"f":"y"}')

    def test_index_defaults_to_zero_when_missing(self):
        buf: dict = {}
        # Some streamers omit `index` on single-call streams — fall
        # back to 0 instead of throwing.
        TinkerClawBackend._accumulate_tool_call_deltas(
            [{"function": {"name": "foo"}}], buf,
        )
        self.assertIn(0, buf)
        self.assertEqual(buf[0]["name"], "foo")


class TestFlushToolCalls(unittest.IsolatedAsyncioTestCase):
    async def test_fires_callback_with_dragon_shape(self):
        captured: list[dict] = []

        async def cb(payload: dict) -> None:
            captured.append(payload)

        be = _NoInitBackend()
        be._on_tool_call = cb
        buf = {0: {"id": "call_a", "name": "web_search", "arguments": '{"q":"weather"}'}}
        flushed: set = set()
        await be._flush_tool_calls(buf, flushed)

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["tool"], "web_search")
        self.assertEqual(captured[0]["args"], {"q": "weather"})
        self.assertIn(0, flushed)

    async def test_idempotent_on_double_flush(self):
        captured: list[dict] = []

        async def cb(payload: dict) -> None:
            captured.append(payload)

        be = _NoInitBackend()
        be._on_tool_call = cb
        buf = {0: {"id": "x", "name": "t", "arguments": "{}"}}
        flushed: set = set()
        await be._flush_tool_calls(buf, flushed)
        await be._flush_tool_calls(buf, flushed)  # second call → no-op
        self.assertEqual(len(captured), 1)

    async def test_no_handler_marks_flushed_but_no_calls(self):
        be = _NoInitBackend()
        buf = {0: {"id": "x", "name": "t", "arguments": "{}"}}
        flushed: set = set()
        await be._flush_tool_calls(buf, flushed)
        self.assertEqual(flushed, {0})  # marked

    async def test_malformed_args_falls_back_to_raw(self):
        captured: list[dict] = []

        async def cb(payload: dict) -> None:
            captured.append(payload)

        be = _NoInitBackend()
        be._on_tool_call = cb
        # Truncated mid-JSON — common when the upstream crashes mid-call
        buf = {0: {"id": "x", "name": "search", "arguments": '{"q":"hello'}}
        flushed: set = set()
        await be._flush_tool_calls(buf, flushed)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["tool"], "search")
        self.assertIn("_raw", captured[0]["args"])

    async def test_callback_exception_is_swallowed(self):
        async def boom(_payload: dict) -> None:
            raise RuntimeError("buggy emitter")

        be = _NoInitBackend()
        be._on_tool_call = boom
        buf = {0: {"id": "x", "name": "t", "arguments": "{}"}}
        flushed: set = set()
        # Must not raise — a buggy callback cannot tear down the LLM
        # stream that's still surfacing tokens to the user.
        await be._flush_tool_calls(buf, flushed)
        self.assertIn(0, flushed)

    async def test_empty_name_skipped(self):
        captured: list[dict] = []

        async def cb(payload: dict) -> None:
            captured.append(payload)

        be = _NoInitBackend()
        be._on_tool_call = cb
        buf = {0: {"id": "x", "name": "  ", "arguments": "{}"}}
        flushed: set = set()
        await be._flush_tool_calls(buf, flushed)
        self.assertEqual(captured, [])  # never emit a nameless tool


class TestSetToolEventHandler(unittest.TestCase):
    def test_setter_stores_and_clears(self):
        be = _NoInitBackend()
        self.assertIsNone(be._on_tool_call)

        async def h(_p: dict) -> None:
            pass

        be.set_tool_event_handler(h)
        self.assertIs(be._on_tool_call, h)
        be.set_tool_event_handler(None)
        self.assertIsNone(be._on_tool_call)


class TestAgentLogBridge(unittest.IsolatedAsyncioTestCase):
    """W7-A.b: gateway tool calls land in the cross-session agent_log
    ring (the /api/v1/agent_log feed Tab5 reads in Wave 12).  Pre-W7-A.b
    the ToolRegistry.execute chokepoint was the only writer, so mode 3
    was dark for the agent_log surface."""

    async def asyncSetUp(self):
        # The ring is process-global; snapshot + restore so tests don't
        # cross-contaminate one another.
        from dragon_voice.api import agent_log as _alog
        self._alog = _alog
        # Save state
        with _alog._lock:
            self._saved_ring = list(_alog._ring)
            self._saved_next = _alog._next_id
            _alog._ring.clear()
            _alog._next_id = 1

    async def asyncTearDown(self):
        with self._alog._lock:
            self._alog._ring.clear()
            for item in self._saved_ring:
                self._alog._ring.append(item)
            self._alog._next_id = self._saved_next

    async def test_flush_records_to_agent_log(self):
        be = _NoInitBackend()
        buf = {0: {"id": "x", "name": "web_search", "arguments": '{"q":"hi"}'}}
        await be._flush_tool_calls(buf, set())
        with self._alog._lock:
            entries = list(self._alog._ring)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["tool"], "web_search")
        self.assertEqual(entries[0]["args"], {"q": "hi"})
        self.assertEqual(entries[0]["status"], "running")

    async def test_records_even_without_handler(self):
        # The W7-A.b agent_log write must NOT depend on a handler being
        # registered — it should fire regardless so /api/v1/agent_log is
        # useful even when no WS-emit callback is wired.
        be = _NoInitBackend()
        self.assertIsNone(be._on_tool_call)
        buf = {0: {"id": "x", "name": "remember", "arguments": '{"fact":"y"}'}}
        await be._flush_tool_calls(buf, set())
        with self._alog._lock:
            entries = list(self._alog._ring)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["tool"], "remember")

    async def test_double_flush_records_once(self):
        be = _NoInitBackend()
        buf = {0: {"id": "x", "name": "t", "arguments": "{}"}}
        flushed: set = set()
        await be._flush_tool_calls(buf, flushed)
        await be._flush_tool_calls(buf, flushed)
        with self._alog._lock:
            entries = list(self._alog._ring)
        self.assertEqual(len(entries), 1)


if __name__ == "__main__":
    unittest.main()
