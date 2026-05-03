"""Tests for ``dragon_voice.tool_event_emitter.ToolEventEmitter``.

Pin every branch of the three async methods + the per-turn tracker
bookkeeping + the web_search auto-widget emit + the ws.closed
short-circuits.

Three groups of tests:

  * `TestOnToolCall` — pre-register the call into tracker,
    pair-emit, ws.closed skip.
  * `TestOnToolResult` — pair-emit, tracker merge, web_search
    widget auto-emit, ws.closed skip.
  * `TestOnToolError` — γ2-M1 structured error pair-emit, B4
    code/message override, ws.closed skip.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.tool_event_emitter import ToolEventEmitter


def _make_ws(*, closed: bool = False) -> MagicMock:
    ws = MagicMock()
    ws.closed = closed
    ws.send_json = AsyncMock()
    return ws


def _make_safe_send_json() -> AsyncMock:
    return AsyncMock(return_value=True)


def _make_emitter(*, closed: bool = False, conn_state: dict | None = None,
                  emit_legacy: bool = True, session_id: str = "sess-XYZ"):
    ws = _make_ws(closed=closed)
    send = _make_safe_send_json()
    state = conn_state if conn_state is not None else {}
    em = ToolEventEmitter(
        ws=ws,
        conn_state=state,
        session_id=session_id,
        safe_send_json=send,
        emit_legacy=emit_legacy,
    )
    return em, ws, send, state


# ─── on_tool_call ────────────────────────────────────────────────


class TestOnToolCall:
    @pytest.mark.asyncio
    async def test_pre_registers_call_in_tracker(self):
        em, ws, send, state = _make_emitter()

        await em.on_tool_call({"tool": "remember", "args": {"fact": "x"}})

        # Pre-register lands in tool_calls_this_turn
        assert state["tool_calls_this_turn"] == [
            {"tool": "remember", "args": {"fact": "x"}},
        ]

    @pytest.mark.asyncio
    async def test_emits_pair_frame_legacy_plus_progress(self):
        """When emit_legacy=True (default), pair-emits both the
        legacy `tool_call` and the new `progress.tool.start`
        frames."""
        em, ws, send, state = _make_emitter(emit_legacy=True)

        await em.on_tool_call({"tool": "datetime", "args": {}})

        # Two sends: legacy + progress
        assert send.await_count == 2
        legacy = send.await_args_list[0].args[1]
        progress = send.await_args_list[1].args[1]
        assert legacy["type"] == "tool_call"
        assert legacy["tool"] == "datetime"
        assert progress.get("type") == "progress"

    @pytest.mark.asyncio
    async def test_emit_legacy_false_skips_legacy_frame(self):
        em, ws, send, state = _make_emitter(emit_legacy=False)

        await em.on_tool_call({"tool": "datetime", "args": {}})

        # Only progress frame, no legacy.
        assert send.await_count == 1
        assert send.await_args.args[1].get("type") == "progress"

    @pytest.mark.asyncio
    async def test_ws_closed_skips_emit_but_still_pre_registers(self):
        """Pre-extract behaviour: even if ws is closed, we still
        update the tracker so the wrap synthesiser sees a complete
        picture for the turn."""
        em, ws, send, state = _make_emitter(closed=True)

        await em.on_tool_call({"tool": "remember", "args": {"fact": "x"}})

        # Tracker still updated.
        assert state["tool_calls_this_turn"] == [
            {"tool": "remember", "args": {"fact": "x"}},
        ]
        # No WS sends.
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_args_none_normalized_to_empty_dict(self):
        """Defensive: pre-extract used `call.get("args") or {}` to
        avoid storing None in the tracker."""
        em, ws, send, state = _make_emitter()
        await em.on_tool_call({"tool": "datetime", "args": None})
        assert state["tool_calls_this_turn"] == [
            {"tool": "datetime", "args": {}},
        ]


# ─── on_tool_result ──────────────────────────────────────────────


class TestOnToolResult:
    @pytest.mark.asyncio
    async def test_emits_pair_frame_with_result_fields(self):
        em, ws, send, state = _make_emitter()

        await em.on_tool_result({
            "tool": "datetime",
            "result": {"now": "2026-05-03T12:00"},
            "execution_ms": 4,
        })

        # Two sends: legacy + progress.
        assert send.await_count == 2
        legacy = send.await_args_list[0].args[1]
        # Legacy payload has all fields spread at top level.
        assert legacy["type"] == "tool_result"
        assert legacy["tool"] == "datetime"
        assert legacy["result"] == {"now": "2026-05-03T12:00"}
        assert legacy["execution_ms"] == 4

    @pytest.mark.asyncio
    async def test_merges_result_into_pre_registered_call(self):
        """#75 phase 1b: merge result into the most-recent
        pre-registered call for this tool name."""
        em, ws, send, state = _make_emitter(
            conn_state={"tool_calls_this_turn": [
                {"tool": "remember", "args": {"fact": "X"}},  # pending
            ]},
        )

        await em.on_tool_result({
            "tool": "remember",
            "result": {"stored": True},
            "execution_ms": 12,
        })

        # The pre-registered call now has its result merged in.
        record = state["tool_calls_this_turn"][0]
        assert record == {
            "tool": "remember",
            "args": {"fact": "X"},
            "result": {"stored": True},
            "execution_ms": 12,
        }

    @pytest.mark.asyncio
    async def test_same_tool_twice_maps_one_to_one(self):
        """If a tool fires twice in one turn, the FIRST pending
        slot gets the FIRST result; the second result merges into
        the second pending slot."""
        em, ws, send, state = _make_emitter(
            conn_state={"tool_calls_this_turn": [
                {"tool": "remember", "args": {"fact": "A"}},
                {"tool": "remember", "args": {"fact": "B"}},
            ]},
        )

        await em.on_tool_result({"tool": "remember", "result": "ok-A"})

        # Only the FIRST pending slot got the result.
        assert state["tool_calls_this_turn"][0].get("result") == "ok-A"
        assert "result" not in state["tool_calls_this_turn"][1]

    @pytest.mark.asyncio
    async def test_no_pre_register_appends_bare_result(self):
        """Some code paths emit tool_result without a prior
        tool_call (test paths, REST execute).  The result still
        lands in the tracker so the wrap has something to read."""
        em, ws, send, state = _make_emitter()

        result = {"tool": "calculator", "result": 42, "execution_ms": 1}
        await em.on_tool_result(result)

        assert state["tool_calls_this_turn"] == [result]

    @pytest.mark.asyncio
    async def test_web_search_auto_emits_widget_list(self):
        """v4·D Phase 4c: when the tool is web_search, auto-emit
        a widget_list frame so Tab5 home shows the top hits
        without LLM orchestration."""
        em, ws, send, state = _make_emitter(session_id="abc12345xyz")

        await em.on_tool_result({
            "tool": "web_search",
            "result": {
                "query": "claude opus pricing",
                "results": [
                    {"title": "Anthropic claude-opus-4.7 pricing"},
                    {"title": "OpenRouter Opus rates"},
                    {"title": "Pricing analysis"},
                ],
            },
            "execution_ms": 234,
        })

        # Find the widget_list frame among the sends (legacy +
        # progress + widget_list = 3 sends total).
        widget_calls = [
            c for c in ws.send_json.await_args_list
            if c.args[0].get("type") == "widget_list"
        ]
        assert len(widget_calls) == 1
        widget = widget_calls[0].args[0]
        assert widget["skill_id"] == "web_search"
        assert widget["card_id"].startswith("ws_")
        assert widget["title"] == "claude opus pricing"
        assert len(widget["items"]) == 3

    @pytest.mark.asyncio
    async def test_web_search_no_results_skips_widget_emit(self):
        em, ws, send, state = _make_emitter()
        await em.on_tool_result({
            "tool": "web_search",
            "result": {"query": "x", "results": []},
        })
        widget_calls = [
            c for c in ws.send_json.await_args_list
            if c.args[0].get("type") == "widget_list"
        ]
        assert widget_calls == []

    @pytest.mark.asyncio
    async def test_non_web_search_does_not_auto_emit_widget(self):
        em, ws, send, state = _make_emitter()
        await em.on_tool_result({"tool": "datetime", "result": "now"})
        widget_calls = [
            c for c in ws.send_json.await_args_list
            if c.args[0].get("type") == "widget_list"
        ]
        assert widget_calls == []

    @pytest.mark.asyncio
    async def test_ws_closed_short_circuits_everything(self):
        """If ws is closed, NOTHING happens — no pair-emit, no
        tracker mutation, no widget emit.  Pre-extract behaviour."""
        em, ws, send, state = _make_emitter(closed=True)

        await em.on_tool_result({"tool": "remember", "result": "x"})

        send.assert_not_awaited()
        assert state == {}  # no tracker mutation
        ws.send_json.assert_not_awaited()


# ─── on_tool_error ───────────────────────────────────────────────


class TestOnToolError:
    @pytest.mark.asyncio
    async def test_default_code_and_message_when_err_minimal(self):
        em, ws, send, state = _make_emitter()

        await em.on_tool_error({"name": "foo"})

        # Two sends: legacy γ1 error_event + progress.tool.error.
        assert send.await_count == 2
        legacy = send.await_args_list[0].args[1]
        # Default code from the closure docstring.
        assert legacy["code"] == "tool_args_invalid"
        # Default message includes the tool name.
        assert "foo" in legacy["message"]

    @pytest.mark.asyncio
    async def test_b4_code_and_message_override_honored(self):
        """Audit B4 (#137): caller-supplied code + message override
        the defaults.  ConvEngine uses this to signal e.g.
        tool_call_limit_reached distinct from tool_args_invalid."""
        em, ws, send, state = _make_emitter()

        await em.on_tool_error({
            "name": "foo",
            "code": "tool_call_limit_reached",
            "message": "Max 3 tool calls per turn — stopping.",
        })

        legacy = send.await_args_list[0].args[1]
        assert legacy["code"] == "tool_call_limit_reached"
        assert legacy["message"] == "Max 3 tool calls per turn — stopping."

    @pytest.mark.asyncio
    async def test_severity_and_scope_are_transient_tool(self):
        em, ws, send, state = _make_emitter()
        await em.on_tool_error({"name": "foo"})
        legacy = send.await_args_list[0].args[1]
        assert legacy["severity"] == "transient"
        assert legacy["scope"] == "tool"

    @pytest.mark.asyncio
    async def test_unknown_tool_name_uses_placeholder(self):
        em, ws, send, state = _make_emitter()
        await em.on_tool_error({})  # no name field
        legacy = send.await_args_list[0].args[1]
        assert "(unknown)" in legacy["message"]

    @pytest.mark.asyncio
    async def test_ws_closed_skips_emit(self):
        em, ws, send, state = _make_emitter(closed=True)
        await em.on_tool_error({"name": "foo"})
        send.assert_not_awaited()


# ─── State capture invariant ─────────────────────────────────────


class TestStateCapture:
    """Pin that the emitter holds REFs (not copies) so mutations
    via the conn_state path remain visible inside the emitter.
    Pre-extract this was natural via Python closure semantics; the
    class-based version must preserve it."""

    @pytest.mark.asyncio
    async def test_conn_state_mutation_visible_through_emitter(self):
        state: dict = {}
        ws = _make_ws()
        send = _make_safe_send_json()
        em = ToolEventEmitter(
            ws=ws,
            conn_state=state,
            session_id="sess-X",
            safe_send_json=send,
            emit_legacy=True,
        )
        # External mutation
        state["tool_calls_this_turn"] = [
            {"tool": "remember", "args": {"fact": "EXT"}},
        ]
        # Result merge sees the externally-added pending call.
        await em.on_tool_result({"tool": "remember", "result": "OK"})
        assert state["tool_calls_this_turn"][0]["result"] == "OK"
