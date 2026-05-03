"""Tests for ``dragon_voice.local_text_stream``.

Pin every behaviour of the local-path streaming loop with
mid-stream tool-marker stripping so a future refactor can't
re-introduce the Wave 10 audit #78 leak.

Test classes:

  * ``TestHappyPath`` — plain text passes through unchanged.
  * ``TestCompleteToolBlockStripped`` — a complete
    `<tool>…</args>` block is stripped before flush.
  * ``TestPartialMarkerHeldBack`` — partial markers at the buffer
    tail are NOT flushed.
  * ``TestKeepaliveWiring`` — the `ws_keepalive` factory is
    invoked with `label="local_text"`.
  * ``TestWsClosedSkipsSend`` — when ws closes mid-stream, no
    further `send_json` calls are attempted.
  * ``TestConversationCallbackForwarding`` — `on_tool_*`
    callbacks from `conn_state` are passed through to
    `conversation.process_text_stream`.
  * ``TestEndOfStreamFlushAfterStrip`` — residual complete tool
    block at end-of-stream is stripped before final flush.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.local_text_stream import stream_local_text_with_tool_filter


def _make_ws(*, closed: bool = False) -> MagicMock:
    ws = MagicMock()
    ws.closed = closed
    ws.send_json = AsyncMock()
    return ws


@asynccontextmanager
async def _noop_keepalive(ws, *, label: str):
    yield


def _make_keepalive_factory():
    calls: list[dict] = []

    @asynccontextmanager
    async def _factory(ws, *, label: str):
        calls.append({"label": label, "ws": ws})
        yield

    _factory.calls = calls  # type: ignore[attr-defined]
    return _factory


def _make_conversation(token_stream: list[str]) -> MagicMock:
    """Build a fake ConversationEngine whose `process_text_stream`
    yields the supplied tokens.  Captures the kwargs it was
    called with for assertion."""
    convo = MagicMock()
    captured: dict = {}

    async def _stream(**kwargs):
        captured.update(kwargs)
        for tok in token_stream:
            yield tok

    convo.process_text_stream = _stream
    convo._captured = captured  # type: ignore[attr-defined]
    return convo


def _emitted_texts(ws: MagicMock) -> list[str]:
    """Extract the text fields from all `llm` frames emitted on ws."""
    return [
        c.args[0]["text"] for c in ws.send_json.call_args_list
        if c.args[0].get("type") == "llm"
    ]


# ─── TestHappyPath ─────────────────────────────────────────────


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_plain_text_passes_through_unchanged(self):
        ws = _make_ws()
        convo = _make_conversation(["Hello", " ", "world", "!"])

        full, text = await stream_local_text_with_tool_filter(
            ws,
            conversation=convo,
            session_id="s1",
            content="hi",
            conn_state={},
            ws_keepalive=_noop_keepalive,
        )

        assert full == ["Hello", " ", "world", "!"]
        assert text == "Hello world!"
        # Joined emit equals "Hello world!"
        assert "".join(_emitted_texts(ws)) == "Hello world!"


# ─── TestCompleteToolBlockStripped ─────────────────────────────


class TestCompleteToolBlockStripped:
    @pytest.mark.asyncio
    async def test_complete_block_stripped_before_flush(self):
        """A complete `<tool>…</args>` block landing atomically
        in a single token tick must be stripped before any flush
        downstream of it."""
        ws = _make_ws()
        # The whole block lands in one token so the buffer
        # strip catches it before the partial-marker scan runs.
        tokens = [
            "Hello ",
            "<tool>web_search</tool><args>{\"q\":\"x\"}</args>",
            " world",
        ]
        convo = _make_conversation(tokens)

        full, text = await stream_local_text_with_tool_filter(
            ws,
            conversation=convo,
            session_id="s1",
            content="hi",
            conn_state={},
            ws_keepalive=_noop_keepalive,
        )

        # full_response is the *unfiltered* list — caller uses
        # this for the rich-media gate / receipt.
        assert full == tokens

        # response_text and the joined emit both have the block
        # removed.  Note the regex's `\s*>?` tail consumes one
        # whitespace after `</args>` → "Hello world" (single
        # space) in the joined-strip; mid-stream emits keep both
        # spaces because "Hello " was flushed before the block
        # arrived.  Both behaviours preserved verbatim.
        assert "<tool>" not in text
        assert "</args>" not in text
        assert text == "Hello world"
        joined = "".join(_emitted_texts(ws))
        assert "<tool>" not in joined
        assert "</args>" not in joined
        assert joined == "Hello  world"

    @pytest.mark.asyncio
    async def test_block_with_trailing_extra_gt_stripped(self):
        """Wave 10 audit #78: qwen3 occasionally double-closes
        with `</args>>`.  The extra `>` must come off too."""
        ws = _make_ws()
        tokens = [
            "ok ",
            "<tool>x</tool><args>{}</args>>",  # extra >
            " done",
        ]
        convo = _make_conversation(tokens)

        _, text = await stream_local_text_with_tool_filter(
            ws,
            conversation=convo,
            session_id="s1",
            content="hi",
            conn_state={},
            ws_keepalive=_noop_keepalive,
        )
        assert text == "ok  done"

    @pytest.mark.asyncio
    async def test_case_insensitive_tag_match(self):
        """The strip is case-insensitive on tag names."""
        ws = _make_ws()
        tokens = ["pre ", "<TOOL>x</Tool><Args>{}</ARGS>", " post"]
        convo = _make_conversation(tokens)

        _, text = await stream_local_text_with_tool_filter(
            ws,
            conversation=convo,
            session_id="s1",
            content="hi",
            conn_state={},
            ws_keepalive=_noop_keepalive,
        )
        assert "<TOOL>" not in text
        assert text == "pre post"  # `\s*>?` tail consumes one space


# ─── TestPartialMarkerHeldBack ─────────────────────────────────


class TestPartialMarkerHeldBack:
    @pytest.mark.asyncio
    async def test_partial_opening_marker_held_back(self):
        """A `<tool` partial at the buffer tail must NOT be
        flushed.  Once the complete block lands the strip removes
        it before the next emit.

        This pins the well-behaved case where the partial marker
        is at the *tail* of the buffer (no preceding complete
        opener).  The pre-existing edge where `<tool>x</tool>`
        arrives without `<args>` yet (causing the partial-`</tool`
        scan to flush `<tool>x` as a "safe" prefix) is a known
        imperfection of the pre-extract loop and is preserved
        verbatim by this module — covered by the
        ConversationEngine's own marker-detection layer upstream.
        """
        ws = _make_ws()
        # Stream a partial `<tool` at end of one chunk, then
        # the rest of the complete block in the next chunk.
        tokens = [
            "say ",                               # flush as "say "
            "<tool",                              # partial — hold
            ">x</tool><args>{}</args>",           # completes — strip
            " done",                              # flush
        ]
        convo = _make_conversation(tokens)

        _, text = await stream_local_text_with_tool_filter(
            ws,
            conversation=convo,
            session_id="s1",
            content="hi",
            conn_state={},
            ws_keepalive=_noop_keepalive,
        )

        # `\s*>?` regex tail consumes one whitespace after `</args>`.
        assert text == "say done"
        # No frame ever leaked the partial-or-complete tool marker.
        for emitted in _emitted_texts(ws):
            assert "<tool" not in emitted, (
                f"interim flush leaked tool marker: {emitted!r}"
            )
            assert "</args" not in emitted

    @pytest.mark.asyncio
    async def test_partial_marker_at_safe_text_tail_still_flushes_safe_prefix(self):
        """If pending == 'hello <tool', we must flush 'hello '
        and hold '<tool' — not stall the entire buffer."""
        ws = _make_ws()
        tokens = ["hello <tool", ">x</tool><args>{}</args>"]
        convo = _make_conversation(tokens)

        _, text = await stream_local_text_with_tool_filter(
            ws,
            conversation=convo,
            session_id="s1",
            content="hi",
            conn_state={},
            ws_keepalive=_noop_keepalive,
        )

        # Final output is just the safe prefix.
        assert text == "hello "
        # First emit was the safe prefix only — no `<tool` leak.
        first_emit = _emitted_texts(ws)[0]
        assert first_emit == "hello "


# ─── TestKeepaliveWiring ───────────────────────────────────────


class TestKeepaliveWiring:
    @pytest.mark.asyncio
    async def test_keepalive_invoked_with_local_text_label(self):
        ws = _make_ws()
        keepalive = _make_keepalive_factory()
        convo = _make_conversation(["x"])

        await stream_local_text_with_tool_filter(
            ws,
            conversation=convo,
            session_id="s1",
            content="hi",
            conn_state={},
            ws_keepalive=keepalive,
        )
        assert keepalive.calls == [{"label": "local_text", "ws": ws}]


# ─── TestWsClosedSkipsSend ─────────────────────────────────────


class TestWsClosedSkipsSend:
    @pytest.mark.asyncio
    async def test_ws_closed_before_stream_skips_all_sends(self):
        ws = _make_ws(closed=True)
        convo = _make_conversation(["hello", " world"])

        full, text = await stream_local_text_with_tool_filter(
            ws,
            conversation=convo,
            session_id="s1",
            content="hi",
            conn_state={},
            ws_keepalive=_noop_keepalive,
        )
        # Tokens still collected — caller still gets full_response.
        assert full == ["hello", " world"]
        assert text == "hello world"
        # But no frames were sent.
        ws.send_json.assert_not_called()


# ─── TestConversationCallbackForwarding ────────────────────────


class TestConversationCallbackForwarding:
    @pytest.mark.asyncio
    async def test_tool_callbacks_from_conn_state_passed_through(self):
        """`on_tool_call` / `on_tool_result` / `on_tool_error`
        from conn_state must reach
        ConversationEngine.process_text_stream so the WS dispatcher
        can surface tool events to Tab5."""
        ws = _make_ws()
        convo = _make_conversation([])

        on_call = MagicMock()
        on_result = MagicMock()
        on_error = MagicMock()

        await stream_local_text_with_tool_filter(
            ws,
            conversation=convo,
            session_id="sess-1",
            content="payload-text",
            conn_state={
                "on_tool_call": on_call,
                "on_tool_result": on_result,
                "on_tool_error": on_error,
            },
            ws_keepalive=_noop_keepalive,
        )

        # Captured kwargs include the callbacks + session/content.
        cap = convo._captured
        assert cap["session_id"] == "sess-1"
        assert cap["text"] == "payload-text"
        assert cap["input_mode"] == "text"
        assert cap["on_tool_call"] is on_call
        assert cap["on_tool_result"] is on_result
        assert cap["on_tool_error"] is on_error

    @pytest.mark.asyncio
    async def test_missing_callbacks_pass_through_as_none(self):
        """conn_state without callback keys → None passed through
        (process_text_stream's default behaviour)."""
        ws = _make_ws()
        convo = _make_conversation([])

        await stream_local_text_with_tool_filter(
            ws,
            conversation=convo,
            session_id="s",
            content="x",
            conn_state={},
            ws_keepalive=_noop_keepalive,
        )

        cap = convo._captured
        assert cap["on_tool_call"] is None
        assert cap["on_tool_result"] is None
        assert cap["on_tool_error"] is None


# ─── TestEndOfStreamFlushAfterStrip ────────────────────────────


class TestEndOfStreamFlushAfterStrip:
    @pytest.mark.asyncio
    async def test_residual_complete_block_stripped_at_end(self):
        """If the *last* token completes a held-back block, the
        end-of-stream strip must remove it before the final
        flush — otherwise a complete block could leak in the
        final frame."""
        ws = _make_ws()
        # Hold "<tool" through to the end, then complete it in
        # the very last token.
        tokens = ["<tool", ">x</tool><args>{}</args>"]
        convo = _make_conversation(tokens)

        _, text = await stream_local_text_with_tool_filter(
            ws,
            conversation=convo,
            session_id="s1",
            content="hi",
            conn_state={},
            ws_keepalive=_noop_keepalive,
        )

        # Nothing leaks anywhere.
        assert text == ""
        for emitted in _emitted_texts(ws):
            assert "<tool" not in emitted
            assert "</args" not in emitted

    @pytest.mark.asyncio
    async def test_end_of_stream_residual_text_flushes_when_safe(self):
        """If the held-back tail at end-of-stream is plain text
        (not part of a tool block), flush it on exit."""
        ws = _make_ws()
        # Put a non-tool marker prefix at the tail that the
        # partial-marker scan won't catch.
        tokens = ["hello ", "world"]
        convo = _make_conversation(tokens)

        _, text = await stream_local_text_with_tool_filter(
            ws,
            conversation=convo,
            session_id="s1",
            content="hi",
            conn_state={},
            ws_keepalive=_noop_keepalive,
        )
        assert text == "hello world"
        assert "".join(_emitted_texts(ws)) == "hello world"
