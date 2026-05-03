"""Tests for ``dragon_voice.text_turn_gate``.

Pin every gate branch + the B1 turn-busy bracket invariants
that prevent widget/token interleave from regressing.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.errors import Scope, Severity
from dragon_voice.text_turn_gate import invoke_with_text_turn_gate


def _make_ws(*, closed: bool = False) -> MagicMock:
    ws = MagicMock()
    ws.closed = closed
    ws.send_json = AsyncMock()
    return ws


def _make_surface_mgr() -> MagicMock:
    mgr = MagicMock()
    mgr.mark_turn_start = MagicMock()
    mgr.mark_turn_end = AsyncMock()
    return mgr


def _make_body() -> AsyncMock:
    return AsyncMock()


# ─── Precondition guards ─────────────────────────────────────


class TestPreconditionGuards:
    @pytest.mark.asyncio
    async def test_no_session_id_emits_fatal_session_invalid(self):
        ws = _make_ws()
        body = _make_body()

        await invoke_with_text_turn_gate(
            ws,
            conn_state={},  # no session_id
            cmd={"content": "hi"},
            conversation=MagicMock(),
            surface_mgr=_make_surface_mgr(),
            body_fn=body,
        )

        ws.send_json.assert_awaited_once()
        frame = ws.send_json.await_args.args[0]
        assert frame["type"] == "error"
        assert frame["code"] == "session_invalid"
        assert frame["severity"] == Severity.FATAL.value
        assert frame["scope"] == Scope.SESSION.value
        body.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_conversation_emits_fatal(self):
        """Conversation engine missing (boot race) → same error."""
        ws = _make_ws()
        body = _make_body()

        await invoke_with_text_turn_gate(
            ws,
            conn_state={"session_id": "s"},
            cmd={"content": "hi"},
            conversation=None,
            surface_mgr=_make_surface_mgr(),
            body_fn=body,
        )

        ws.send_json.assert_awaited_once()
        body.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_content_silent_return(self):
        ws = _make_ws()
        body = _make_body()

        await invoke_with_text_turn_gate(
            ws,
            conn_state={"session_id": "s"},
            cmd={"content": ""},
            conversation=MagicMock(),
            surface_mgr=_make_surface_mgr(),
            body_fn=body,
        )

        # Silent — no error frame
        ws.send_json.assert_not_awaited()
        body.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_whitespace_only_content_silent_return(self):
        ws = _make_ws()
        body = _make_body()

        await invoke_with_text_turn_gate(
            ws,
            conn_state={"session_id": "s"},
            cmd={"content": "   \n\t  "},
            conversation=MagicMock(),
            surface_mgr=_make_surface_mgr(),
            body_fn=body,
        )

        ws.send_json.assert_not_awaited()
        body.assert_not_awaited()


# ─── Per-turn tool tracker reset (#75 phase 1b) ──────────────


class TestPerTurnToolTrackerReset:
    @pytest.mark.asyncio
    async def test_tool_tracker_reset_before_body(self):
        """conn_state['tool_calls_this_turn'] must be cleared
        before body_fn runs so the body can append fresh entries."""
        ws = _make_ws()
        # Simulate a previous turn's leftover tool calls
        conn_state = {
            "session_id": "s",
            "tool_calls_this_turn": [{"name": "old_tool"}],
        }

        captured_state = {}

        async def _body(ws_arg, cs, cmd_arg, text, sid, content):
            # Snapshot the tracker state at body entry
            captured_state["at_entry"] = list(cs["tool_calls_this_turn"])

        await invoke_with_text_turn_gate(
            ws,
            conn_state=conn_state,
            cmd={"content": "fresh turn"},
            conversation=MagicMock(),
            surface_mgr=None,
            body_fn=_body,
        )

        # Body saw an empty list (reset before body ran)
        assert captured_state["at_entry"] == []


# ─── B1 turn-busy bracket (audit B1 / #165) ──────────────────


class TestB1TurnBusyBracket:
    @pytest.mark.asyncio
    async def test_mark_turn_start_called_before_body(self):
        ws = _make_ws()
        surface = _make_surface_mgr()
        order: list[str] = []

        surface.mark_turn_start.side_effect = (
            lambda sid: order.append(f"start:{sid}")
        )

        async def _body(*args):
            order.append("body")

        await invoke_with_text_turn_gate(
            ws,
            conn_state={"session_id": "s-A"},
            cmd={"content": "x"},
            conversation=MagicMock(),
            surface_mgr=surface,
            body_fn=_body,
        )

        # mark_turn_start fires BEFORE body
        assert order[:2] == ["start:s-A", "body"]

    @pytest.mark.asyncio
    async def test_mark_turn_end_called_after_body(self):
        ws = _make_ws()
        surface = _make_surface_mgr()
        order: list[str] = []

        async def _body(*args):
            order.append("body")

        async def _record_end(sid):
            order.append(f"end:{sid}")

        surface.mark_turn_end.side_effect = _record_end

        await invoke_with_text_turn_gate(
            ws,
            conn_state={"session_id": "s-B"},
            cmd={"content": "x"},
            conversation=MagicMock(),
            surface_mgr=surface,
            body_fn=_body,
        )

        # mark_turn_end fires AFTER body
        assert order == ["body", "end:s-B"]

    @pytest.mark.asyncio
    async def test_mark_turn_end_fires_even_when_body_raises(self):
        """try/finally invariant: mark_turn_end MUST fire even if
        body raises, otherwise the next turn would be perpetually
        deferred (audit B1 / #165 regression)."""
        ws = _make_ws()
        surface = _make_surface_mgr()

        async def _broken(*args):
            raise RuntimeError("body bug")

        # Body's RuntimeError should propagate out (the wrapper
        # doesn't swallow body exceptions — that's the dispatcher
        # task's responsibility to handle).
        with pytest.raises(RuntimeError, match="body bug"):
            await invoke_with_text_turn_gate(
                ws,
                conn_state={"session_id": "s-X"},
                cmd={"content": "x"},
                conversation=MagicMock(),
                surface_mgr=surface,
                body_fn=_broken,
            )

        # But mark_turn_end MUST have fired despite the body raising.
        surface.mark_turn_end.assert_awaited_once_with("s-X")

    @pytest.mark.asyncio
    async def test_mark_turn_end_failure_does_not_propagate(self):
        """surface_mgr.mark_turn_end raising must NOT propagate —
        a bug in surface mgr must not block the next text turn."""
        ws = _make_ws()
        surface = MagicMock()
        surface.mark_turn_start = MagicMock()
        surface.mark_turn_end = AsyncMock(
            side_effect=RuntimeError("surface bug"),
        )

        async def _body(*args):
            pass

        # Must NOT raise.
        await invoke_with_text_turn_gate(
            ws,
            conn_state={"session_id": "s"},
            cmd={"content": "x"},
            conversation=MagicMock(),
            surface_mgr=surface,
            body_fn=_body,
        )

    @pytest.mark.asyncio
    async def test_no_surface_mgr_skips_bracket(self):
        """Test path / boot race — silent skip when surface_mgr
        unavailable.  Body still runs."""
        ws = _make_ws()
        body = _make_body()

        await invoke_with_text_turn_gate(
            ws,
            conn_state={"session_id": "s"},
            cmd={"content": "x"},
            conversation=MagicMock(),
            surface_mgr=None,
            body_fn=body,
        )

        body.assert_awaited_once()


# ─── Body invocation ─────────────────────────────────────────


class TestBodyInvocation:
    @pytest.mark.asyncio
    async def test_body_receives_canonical_args(self):
        ws = _make_ws()
        body = _make_body()
        cmd = {"content": "  hello world  "}

        await invoke_with_text_turn_gate(
            ws,
            conn_state={"session_id": "sess-1"},
            cmd=cmd,
            conversation=MagicMock(),
            surface_mgr=None,
            body_fn=body,
        )

        body.assert_awaited_once()
        args = body.await_args.args
        # (ws, conn_state, cmd, text, session_id, content)
        assert args[0] is ws
        assert args[2] is cmd
        assert args[3] == "hello world"     # text (stripped)
        assert args[4] == "sess-1"          # session_id
        assert args[5] == "hello world"     # content (stripped)

    @pytest.mark.asyncio
    async def test_text_and_content_are_both_the_stripped_value(self):
        """Pin the pre-extract behaviour: text=content, both equal
        to the stripped cmd['content'].  A future refactor that
        starts mutating one of them in the wrapper would surprise
        downstream callers (TC bypass uses `text`, local path
        uses `content`)."""
        ws = _make_ws()
        body = _make_body()

        await invoke_with_text_turn_gate(
            ws,
            conn_state={"session_id": "s"},
            cmd={"content": "  hi  "},
            conversation=MagicMock(),
            surface_mgr=None,
            body_fn=body,
        )

        args = body.await_args.args
        assert args[3] == args[5] == "hi"
