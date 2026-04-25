"""Unit tests for `ToolRegistry.parse_tool_calls` / `has_tool_call`.

Covers all three accepted dialects and verifies that user prose that
*looks* like a bracket-tag tool call (but doesn't match a registered
tool, or doesn't have valid args) does NOT false-positive.

Dialect 3 (xLAM bracket-name) is the headline addition from issue #82
— see registry.py module docstring for the dialect taxonomy and PR #74
for dialects 1 + 2.
"""

from __future__ import annotations

import asyncio

from dragon_voice.tools.base import Tool
from dragon_voice.tools.registry import ToolRegistry


class _FakeTool(Tool):
    """Minimal Tool stub — the parser only cares about `.name` so the
    registry's name set is populated correctly for dialect-3
    validation."""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"fake tool {self._name}"

    @property
    def parameters_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, args: dict) -> dict:  # pragma: no cover
        return {"ok": True}


def _registry_with(*names: str) -> ToolRegistry:
    r = ToolRegistry()
    for n in names:
        r.register(_FakeTool(n))
    return r


# ───────────────────────── Dialect 1 — legacy <tool>NAME</tool><args>{…}</args>


def test_dialect1_clean_call() -> None:
    r = _registry_with("calculator")
    out = r.parse_tool_calls(
        '<tool>calculator</tool><args>{"expression": "2+2"}</args>'
    )
    assert out == [{"tool": "calculator", "args": {"expression": "2+2"}}]
    assert r.has_tool_call(
        '<tool>calculator</tool><args>{"expression": "2+2"}</args>'
    )


def test_dialect1_xlam_bracket_quirk_open_tag() -> None:
    # All four bracket permutations on the opening tag must parse.
    r = _registry_with("recall")
    for opener in ("<tool>", "[tool>", "<tool]", "[tool]"):
        text = f'{opener}recall</tool><args>{{"query": "x"}}</args>'
        out = r.parse_tool_calls(text)
        assert out == [{"tool": "recall", "args": {"query": "x"}}], opener


def test_dialect1_handles_nested_json_args() -> None:
    # The JSON walker (not the `{.*?}` non-greedy regex) must survive
    # nested objects that the original parser truncated at the first `}`.
    r = _registry_with("memory")
    out = r.parse_tool_calls(
        '<tool>memory</tool><args>{"filter": {"k": "v"}, "limit": 5}</args>'
    )
    assert out == [
        {
            "tool": "memory",
            "args": {"filter": {"k": "v"}, "limit": 5},
        }
    ]


# ───────────────────────── Dialect 2 — <tool_call>{json}</tool_call>


def test_dialect2_clean_call_arguments_key() -> None:
    r = _registry_with("web_search")
    out = r.parse_tool_calls(
        '<tool_call>{"name": "web_search", "arguments": {"query": "esp32"}}</tool_call>'
    )
    assert out == [{"tool": "web_search", "args": {"query": "esp32"}}]


def test_dialect2_accepts_args_alias_for_arguments() -> None:
    r = _registry_with("datetime")
    out = r.parse_tool_calls(
        '<tool_call>{"name": "datetime", "args": {}}</tool_call>'
    )
    assert out == [{"tool": "datetime", "args": {}}]


def test_dialect2_skips_call_with_missing_name() -> None:
    r = _registry_with("anything")
    out = r.parse_tool_calls(
        '<tool_call>{"arguments": {"x": 1}}</tool_call>'
    )
    assert out == []


# ───────────────────────── Dialect 3 — bracket-name xLAM quirk (#82)


def test_dialect3a_json_args_with_close_tag() -> None:
    # Canonical FC dialect xLAM emits — JSON args + matching close.
    r = _registry_with("recall")
    out = r.parse_tool_calls(
        '[recall]{"query": "How much disk space is left?"}</recall>'
    )
    assert out == [
        {"tool": "recall", "args": {"query": "How much disk space is left?"}}
    ]
    assert r.has_tool_call(
        '[recall]{"query": "x"}</recall>'
    )


def test_dialect3a_json_args_without_close_tag_still_accepted() -> None:
    # xLAM truncates the close tag on some prompts — be tolerant.
    r = _registry_with("calculator")
    out = r.parse_tool_calls('[calculator]{"expression": "2+2"}')
    assert out == [{"tool": "calculator", "args": {"expression": "2+2"}}]


def test_dialect3b_function_call_style_noise_with_empty_args() -> None:
    # The `[datetime]GETCURRENTDATEANDTIME()` form xLAM emitted in the
    # dual-pipeline bench.  Treat as call with empty args.
    r = _registry_with("datetime")
    out = r.parse_tool_calls("[datetime]GETCURRENTDATEANDTIME()")
    assert out == [{"tool": "datetime", "args": {}}]


def test_dialect3_unknown_name_does_not_fire() -> None:
    # No `note` registered — `[note]` in user prose must NOT trigger.
    r = _registry_with("calculator")
    text = "I want to add [note] this for later, please remember it."
    assert r.parse_tool_calls(text) == []
    assert not r.has_tool_call(text)


def test_dialect3_known_name_in_prose_without_payload_does_not_fire() -> None:
    # `[recall]` mentioned in conversation but with no `{...}` JSON or
    # `IDENT()` after it — must NOT false-positive.
    r = _registry_with("recall")
    text = "The user mentioned [recall] earlier but didn't ask for it."
    assert r.parse_tool_calls(text) == []
    assert not r.has_tool_call(text)


def test_dialect3b_lowercase_noise_rejected() -> None:
    # The noise-args form REQUIRES uppercase identifier (function-name
    # shape) so prose like `[note]some thing()` doesn't fire.
    r = _registry_with("note")
    out = r.parse_tool_calls("[note]some thing()")
    assert out == []


def test_dialect3b_oversize_noise_rejected() -> None:
    # 80-char window only — sentence-length noise can't be a function name.
    r = _registry_with("datetime")
    out = r.parse_tool_calls(
        "[datetime] " + "X" * 90 + "()"
    )
    assert out == []


def test_dialect3_bare_bracket_recall_skipped() -> None:
    # xLAM solo G8 emitted just `[recall]` with no payload at all.
    # That's an unfinished call; we leave it for the responder to handle
    # rather than firing with no args.
    r = _registry_with("recall")
    assert r.parse_tool_calls("[recall]") == []


# ───────────────────────── Cross-dialect / mixed input


def test_mixed_dialects_all_fire() -> None:
    r = _registry_with("calculator", "recall", "datetime")
    text = (
        '<tool>calculator</tool><args>{"expression": "1+1"}</args>'
        ' some prose '
        '<tool_call>{"name": "recall", "arguments": {"query": "x"}}</tool_call>'
        ' more prose '
        '[datetime]GETCURRENTTIME()'
    )
    out = r.parse_tool_calls(text)
    assert {c["tool"] for c in out} == {"calculator", "recall", "datetime"}


def test_has_tool_call_false_on_pure_prose() -> None:
    r = _registry_with("recall", "calculator")
    assert not r.has_tool_call("Hello, how are you today?")
    assert not r.has_tool_call("")


def test_empty_registry_falls_back_safely_on_dialect3() -> None:
    # Without any registered tool, dialect-3 must be a complete no-op
    # (the validation gate would reject every match anyway).  Existing
    # dialects 1 + 2 must still work.
    r = ToolRegistry()
    assert r.parse_tool_calls('[recall]{"query": "x"}</recall>') == []
    assert r.parse_tool_calls(
        '<tool_call>{"name": "x", "arguments": {}}</tool_call>'
    ) == [{"tool": "x", "args": {}}]


# Quick sanity: register and execute path still works end-to-end.
def test_registered_tool_executes() -> None:
    r = _registry_with("calculator")
    out = asyncio.run(r.execute("calculator", {"expression": "2+2"}))
    assert out["tool"] == "calculator"
    assert out["result"] == {"ok": True}
