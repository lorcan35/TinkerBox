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
        self._on_tool_result = None


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

    def test_setter_stores_both_handlers(self):
        """W7-A.2: setter accepts both on_tool_call and on_tool_result."""
        be = _NoInitBackend()

        async def call_h(_p: dict) -> None:
            pass

        async def result_h(_p: dict) -> None:
            pass

        be.set_tool_event_handler(call_h, result_h)
        self.assertIs(be._on_tool_call, call_h)
        self.assertIs(be._on_tool_result, result_h)

        # Clearing both via None
        be.set_tool_event_handler(None, None)
        self.assertIsNone(be._on_tool_call)
        self.assertIsNone(be._on_tool_result)


class TestSyntheticToolResult(unittest.IsolatedAsyncioTestCase):
    """W7-A.2: synthetic tool_result emission.

    Pre-W7-A.2 mode 3's Tab5 chat UI showed a perpetually-spinning
    tool indicator because /v1/chat/completions doesn't natively
    surface tool_result events.  The fix infers completion from the
    "next content burst after a tool_call" boundary."""

    async def asyncSetUp(self):
        # Quarantine the agent_log ring (same pattern as other tests).
        from dragon_voice.api import agent_log as _alog
        self._alog = _alog
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

    async def test_flush_populates_pending_results(self):
        be = _NoInitBackend()
        buf = {0: {"id": "x", "name": "search", "arguments": "{}"}}
        pending: list[str] = []
        await be._flush_tool_calls(buf, set(), pending_results=pending)
        self.assertEqual(pending, ["search"])

    async def test_emit_drains_pending_and_fires_callback(self):
        captured: list[dict] = []

        async def cb(payload: dict) -> None:
            captured.append(payload)

        be = _NoInitBackend()
        be._on_tool_result = cb
        pending = ["web_search", "bash"]
        await be._emit_synthetic_results(pending)
        self.assertEqual(pending, [])  # drained
        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[0]["tool"], "web_search")
        self.assertEqual(captured[1]["tool"], "bash")
        # Payload is the minimal "completed" signal — no actual data.
        self.assertIsNone(captured[0]["result"])
        self.assertIsNone(captured[0]["execution_ms"])

    async def test_emit_marks_agent_log_done(self):
        """Synthetic result must flip the corresponding agent_log
        ring entry from running → done so /api/v1/agent_log is
        accurate post-completion.  W7-A.3: source must match — a
        gateway-side flush only closes gateway-source entries."""
        # Record a running call from the gateway surface (mirroring
        # what W7-A.b's bridge does).
        self._alog.record_call("search", {"q": "weather"}, source="gateway")
        with self._alog._lock:
            initial = list(self._alog._ring)
        self.assertEqual(initial[0]["status"], "running")
        self.assertEqual(initial[0]["source"], "gateway")

        be = _NoInitBackend()
        await be._emit_synthetic_results(["search"])

        with self._alog._lock:
            after = list(self._alog._ring)
        self.assertEqual(after[0]["status"], "done")
        self.assertEqual(after[0]["source"], "gateway")

    async def test_empty_pending_is_noop(self):
        # No callback, no pending → must not raise + must not emit
        captured: list[dict] = []

        async def cb(payload: dict) -> None:
            captured.append(payload)

        be = _NoInitBackend()
        be._on_tool_result = cb
        await be._emit_synthetic_results([])
        self.assertEqual(captured, [])

    async def test_no_handler_still_records_to_agent_log(self):
        """The agent_log surface should work even when the WS callback
        isn't wired (e.g., transient handler clear during backend swap).
        W7-A.3: gateway-source matching."""
        self._alog.record_call("search", {"q": "weather"}, source="gateway")
        be = _NoInitBackend()
        self.assertIsNone(be._on_tool_result)
        await be._emit_synthetic_results(["search"])
        with self._alog._lock:
            after = list(self._alog._ring)
        self.assertEqual(after[0]["status"], "done")

    async def test_callback_exception_swallowed(self):
        async def boom(_p: dict) -> None:
            raise RuntimeError("buggy handler")

        be = _NoInitBackend()
        be._on_tool_result = boom
        # Must not raise — buggy handlers can't tear down the LLM stream.
        await be._emit_synthetic_results(["search"])

    async def test_double_emit_does_not_double_fire(self):
        """Idempotency: a list emptied by one call must produce no
        callbacks on a second call."""
        captured: list[dict] = []

        async def cb(payload: dict) -> None:
            captured.append(payload)

        be = _NoInitBackend()
        be._on_tool_result = cb
        pending = ["search"]
        await be._emit_synthetic_results(pending)
        await be._emit_synthetic_results(pending)
        self.assertEqual(len(captured), 1)


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


# ── W7-G: gateway browser source bucketing ────────────────────────────


class TestGatewayBrowserSourceClassifier(unittest.TestCase):
    """W7-G: pure-function classifier maps gateway tool names to a
    bucketed source string.  Pulled out as a tiny unit so we can lock
    the contract before exercising it end-to-end through the SSE
    flush helpers (covered in TestGatewayBrowserSource below)."""

    def test_bare_browser_is_browser_bucket(self):
        from dragon_voice.llm.tinkerclaw_llm import _classify_gateway_source
        self.assertEqual(_classify_gateway_source("browser"), "gateway_browser")

    def test_underscore_namespace_is_browser_bucket(self):
        from dragon_voice.llm.tinkerclaw_llm import _classify_gateway_source
        # OpenClaw + tooling test fixtures show `browser_actions` as a
        # real surface (see tool-mutation.test.ts).
        self.assertEqual(
            _classify_gateway_source("browser_actions"), "gateway_browser",
        )
        self.assertEqual(
            _classify_gateway_source("browser_open"), "gateway_browser",
        )

    def test_dotted_namespace_is_browser_bucket(self):
        from dragon_voice.llm.tinkerclaw_llm import _classify_gateway_source
        # Forward-compat for "browser.click", "browser.navigate", etc.
        self.assertEqual(
            _classify_gateway_source("browser.click"), "gateway_browser",
        )
        self.assertEqual(
            _classify_gateway_source("browser.navigate"), "gateway_browser",
        )

    def test_case_insensitive_match(self):
        from dragon_voice.llm.tinkerclaw_llm import _classify_gateway_source
        self.assertEqual(_classify_gateway_source("Browser"), "gateway_browser")
        self.assertEqual(_classify_gateway_source("BROWSER"), "gateway_browser")
        self.assertEqual(
            _classify_gateway_source("Browser_Actions"), "gateway_browser",
        )

    def test_unrelated_tools_stay_in_gateway_bucket(self):
        from dragon_voice.llm.tinkerclaw_llm import _classify_gateway_source
        self.assertEqual(_classify_gateway_source("bash"), "gateway")
        self.assertEqual(_classify_gateway_source("web_search"), "gateway")
        self.assertEqual(_classify_gateway_source("remember"), "gateway")
        self.assertEqual(_classify_gateway_source("read_file"), "gateway")
        # `browser`-prefixed but NOT a browser tool — must not false-fire.
        # The rule is exact match or `browser_`/`browser.` separator.
        self.assertEqual(_classify_gateway_source("browserify"), "gateway")
        self.assertEqual(_classify_gateway_source("browseractivity"), "gateway")

    def test_empty_and_none_fall_through_to_gateway(self):
        from dragon_voice.llm.tinkerclaw_llm import _classify_gateway_source
        self.assertEqual(_classify_gateway_source(""), "gateway")
        self.assertEqual(_classify_gateway_source("   "), "gateway")
        # Tolerate None — the SSE buffer can hand us empty names if the
        # upstream stream drops mid-frame.
        self.assertEqual(_classify_gateway_source(None), "gateway")  # type: ignore[arg-type]


class TestGatewayBrowserSourceEndToEnd(unittest.IsolatedAsyncioTestCase):
    """W7-G: when the SSE parser flushes a tool call whose name is
    `browser` (or a `browser_*` / `browser.*` namespace), the
    agent_log ring records source=gateway_browser so /api/v1/agent_log
    can bucket browser activity separately from generic gateway work.
    """

    async def asyncSetUp(self):
        from dragon_voice.api import agent_log as _alog
        self._alog = _alog
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

    async def test_browser_flush_records_browser_source(self):
        be = _NoInitBackend()
        buf = {0: {"id": "x", "name": "browser",
                   "arguments": '{"action":"tabs"}'}}
        await be._flush_tool_calls(buf, set())
        with self._alog._lock:
            entries = list(self._alog._ring)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["tool"], "browser")
        self.assertEqual(entries[0]["source"], "gateway_browser")
        self.assertEqual(entries[0]["status"], "running")

    async def test_browser_actions_flush_records_browser_source(self):
        be = _NoInitBackend()
        buf = {0: {"id": "x", "name": "browser_actions",
                   "arguments": '{"action":"list"}'}}
        await be._flush_tool_calls(buf, set())
        with self._alog._lock:
            entries = list(self._alog._ring)
        self.assertEqual(entries[0]["source"], "gateway_browser")

    async def test_dotted_browser_flush_records_browser_source(self):
        be = _NoInitBackend()
        buf = {0: {"id": "x", "name": "browser.click",
                   "arguments": '{"selector":"#submit"}'}}
        await be._flush_tool_calls(buf, set())
        with self._alog._lock:
            entries = list(self._alog._ring)
        self.assertEqual(entries[0]["source"], "gateway_browser")

    async def test_non_browser_flush_stays_in_gateway_bucket(self):
        # Sanity check: pre-W7-G behavior preserved for non-browser tools.
        be = _NoInitBackend()
        buf = {
            0: {"id": "a", "name": "bash", "arguments": '{"cmd":"ls"}'},
            1: {"id": "b", "name": "web_search", "arguments": '{"q":"x"}'},
        }
        await be._flush_tool_calls(buf, set())
        with self._alog._lock:
            entries = list(self._alog._ring)
        sources = {e["tool"]: e["source"] for e in entries}
        self.assertEqual(sources["bash"], "gateway")
        self.assertEqual(sources["web_search"], "gateway")

    async def test_synthetic_result_uses_browser_source(self):
        # _emit_synthetic_results must classify the same way so the
        # done-marker flips the matching gateway_browser running entry,
        # NOT spawn a synthetic gateway-bucket entry alongside it.
        be = _NoInitBackend()
        # Seed a running browser call.
        buf = {0: {"id": "x", "name": "browser", "arguments": "{}"}}
        await be._flush_tool_calls(buf, set())
        await be._emit_synthetic_results(["browser"])
        with self._alog._lock:
            entries = list(self._alog._ring)
        # Exactly one entry, source=gateway_browser, status=done.
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["source"], "gateway_browser")
        self.assertEqual(entries[0]["status"], "done")

    async def test_mixed_burst_yields_two_source_buckets(self):
        # Realistic mode-3 turn: gateway fires `browser` + `web_search`
        # in the same flush.  agent_log must split them into two
        # source buckets, both tagged as gateway-flavored.
        be = _NoInitBackend()
        buf = {
            0: {"id": "a", "name": "browser",
                "arguments": '{"action":"tabs"}'},
            1: {"id": "b", "name": "web_search",
                "arguments": '{"q":"esp32-p4"}'},
        }
        await be._flush_tool_calls(buf, set())
        with self._alog._lock:
            entries = list(self._alog._ring)
        sources = {e["tool"]: e["source"] for e in entries}
        self.assertEqual(sources["browser"], "gateway_browser")
        self.assertEqual(sources["web_search"], "gateway")


if __name__ == "__main__":
    unittest.main()
