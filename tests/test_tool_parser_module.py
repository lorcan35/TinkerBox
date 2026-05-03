"""Tests for ``dragon_voice.tools.parser`` (the module-level API).

The 27 existing tests in tests/test_tool_parser.py exercise the
parser via ToolRegistry (the backward-compatible call site).
This file exercises the parser as the module-level free
functions directly — pinning the new API contract so callers
that bypass the registry (future skill SDK introspection,
external test harnesses) can rely on the same shape.
"""
from __future__ import annotations

import pytest

from dragon_voice.tools.parser import (
    has_tool_call,
    parse_tool_calls,
    parse_tool_calls_with_errors,
)


# ─── parse_tool_calls (backward-compat list-only) ────────────


class TestParseToolCalls:
    def test_empty_string_returns_empty(self):
        assert parse_tool_calls("", registered_names=set()) == []

    def test_dialect1_basic(self):
        text = '<tool>web_search</tool><args>{"q": "weather"}</args>'
        calls = parse_tool_calls(text, registered_names={"web_search"})
        assert calls == [{"tool": "web_search", "args": {"q": "weather"}}]

    def test_dialect2_basic(self):
        text = '<tool_call>{"name": "datetime", "arguments": {}}</tool_call>'
        calls = parse_tool_calls(text, registered_names={"datetime"})
        assert calls == [{"tool": "datetime", "args": {}}]

    def test_dialect3_basic(self):
        text = '[recall]{"query": "user prefs"}</recall>'
        calls = parse_tool_calls(text, registered_names={"recall"})
        assert calls == [{"tool": "recall", "args": {"query": "user prefs"}}]


# ─── parse_tool_calls_with_errors (γ2-M1 surfacing) ──────────


class TestParseToolCallsWithErrors:
    def test_returns_tuple_of_calls_and_errors(self):
        result = parse_tool_calls_with_errors(
            "", registered_names=set(),
        )
        assert isinstance(result, tuple)
        assert len(result) == 2
        calls, errors = result
        assert calls == []
        assert errors == []

    def test_dialect1_json_decode_failure_surfaced(self):
        text = '<tool>x</tool><args>{not json}</args>'
        calls, errors = parse_tool_calls_with_errors(
            text, registered_names={"x"},
        )
        assert calls == []
        assert errors == [
            {"dialect": 1, "name": "x", "reason": "json_decode"},
        ]

    def test_dialect2_json_decode_failure_surfaced(self):
        text = '<tool_call>{not json}</tool_call>'
        calls, errors = parse_tool_calls_with_errors(
            text, registered_names=set(),
        )
        assert calls == []
        assert errors == [
            {"dialect": 2, "name": None, "reason": "json_decode"},
        ]

    def test_dialect3_json_decode_failure_surfaced(self):
        text = '[recall]{not json}</recall>'
        calls, errors = parse_tool_calls_with_errors(
            text, registered_names={"recall"},
        )
        assert calls == []
        assert errors == [
            {"dialect": 3, "name": "recall", "reason": "json_decode"},
        ]


# ─── Dialect 3 registry-name validation gate ─────────────────


class TestDialect3NameGate:
    def test_unregistered_name_silently_dropped(self):
        """Pin: bracket-name pre-check that doesn't match a
        registered tool MUST NOT enter the parse path — otherwise
        prose like `[note]` quoted in a chat reply would over-fire."""
        text = '[note]{"text": "hi"}</note>'
        calls = parse_tool_calls(
            text, registered_names={"web_search"},  # `note` NOT registered
        )
        assert calls == []

    def test_empty_registry_skips_dialect3_entirely(self):
        text = '[anything]{"key": "value"}</anything>'
        calls, errors = parse_tool_calls_with_errors(
            text, registered_names=set(),
        )
        assert calls == []
        # Even malformed dialect-3 args won't surface errors when
        # the registry is empty (we skip the whole pass).
        assert errors == []


# ─── Nested-JSON brace walker (audit P2 fix) ────────────────


class TestNestedJsonWalker:
    def test_dialect1_with_nested_json_args_preserved(self):
        """audit P2: pre-fix the regex `{.*?}` truncated at the
        first inner `}` and lost the outer closer."""
        text = '<tool>x</tool><args>{"filter": {"k": "v"}}</args>'
        calls = parse_tool_calls(text, registered_names={"x"})
        assert calls == [
            {"tool": "x", "args": {"filter": {"k": "v"}}},
        ]

    def test_dialect2_with_nested_json_arguments_preserved(self):
        text = (
            '<tool_call>{"name": "x", '
            '"arguments": {"filter": {"k": {"deep": "v"}}}}</tool_call>'
        )
        calls = parse_tool_calls(text, registered_names={"x"})
        assert calls == [
            {"tool": "x", "args": {"filter": {"k": {"deep": "v"}}}},
        ]


# ─── xLAM open-tag bracket quirks ────────────────────────────


class TestXlamBracketQuirks:
    @pytest.mark.parametrize("open_tag", [
        "<tool>", "[tool>", "<tool]", "[tool]",
    ])
    def test_dialect1_accepts_all_four_open_tag_combos(self, open_tag):
        text = f'{open_tag}x</tool><args>{{"k": "v"}}</args>'
        calls = parse_tool_calls(text, registered_names={"x"})
        assert calls == [{"tool": "x", "args": {"k": "v"}}]


# ─── has_tool_call ───────────────────────────────────────────


class TestHasToolCall:
    def test_empty_string_false(self):
        assert has_tool_call("", registered_names=set()) is False

    def test_dialect1_marker_true(self):
        assert has_tool_call(
            "<tool>x</tool>", registered_names=set(),
        ) is True

    def test_dialect2_marker_true(self):
        assert has_tool_call(
            "<tool_call>{}</tool_call>", registered_names=set(),
        ) is True

    def test_dialect3_with_registered_name_true(self):
        assert has_tool_call(
            '[recall]{"q": "x"}', registered_names={"recall"},
        ) is True

    def test_dialect3_with_unregistered_name_false(self):
        """Pin: prose like `[note]` quoted in a chat reply MUST
        NOT register as a tool call when `note` isn't a registered
        tool."""
        assert has_tool_call(
            '[note]{"x": 1}', registered_names={"web_search"},
        ) is False

    def test_dialect3_bracket_name_without_following_brace_or_paren_false(self):
        """`[recall] text follows` is prose — the bracket-name
        peek-ahead must distinguish from a real call."""
        assert has_tool_call(
            "[recall] is what I just did",
            registered_names={"recall"},
        ) is False

    def test_dialect3_bracket_name_with_paren_form_true(self):
        """xLAM sub-form B: `[NAME]IDENT()` — empty args."""
        assert has_tool_call(
            "[datetime] GETCURRENTTIME()",
            registered_names={"datetime"},
        ) is True


# ─── Multi-call extraction ──────────────────────────────────


class TestMultiCall:
    def test_two_dialect1_calls_in_one_text(self):
        text = (
            '<tool>a</tool><args>{"k":1}</args>'
            ' some text '
            '<tool>b</tool><args>{"k":2}</args>'
        )
        calls = parse_tool_calls(text, registered_names={"a", "b"})
        assert calls == [
            {"tool": "a", "args": {"k": 1}},
            {"tool": "b", "args": {"k": 2}},
        ]

    def test_mixed_dialects_in_one_text(self):
        text = (
            '<tool>a</tool><args>{"k":1}</args>'
            '<tool_call>{"name":"b", "arguments":{"k":2}}</tool_call>'
        )
        calls = parse_tool_calls(text, registered_names={"a", "b"})
        names = [c["tool"] for c in calls]
        assert "a" in names and "b" in names
