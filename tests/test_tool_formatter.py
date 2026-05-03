"""Tests for ``dragon_voice.tools.formatter``.

Pin every formatter branch + the priority-tool list (issue #134
closure) so a future refactor can't silently regress to losing
schedule_reminder from the compact format.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from dragon_voice.tools.formatter import (
    COMPACT_PRIORITY_TOOLS,
    format_for_llm,
)


def _make_tool(
    name: str,
    *,
    description: str = "test desc",
    properties: dict | None = None,
    required: list[str] | None = None,
) -> MagicMock:
    tool = MagicMock()
    tool.name = name
    tool.description = description
    tool.parameters_schema = {
        "properties": properties or {},
        "required": required or [],
    }
    return tool


# ─── Empty input ─────────────────────────────────────────────


class TestEmpty:
    def test_empty_tools_returns_empty_string(self):
        assert format_for_llm([]) == ""
        assert format_for_llm([], compact=True) == ""


# ─── Compact format ──────────────────────────────────────────


class TestCompactFormat:
    def test_compact_uses_priority_subset(self):
        """Pin: only priority tools render in the compact format
        even when the registry has more tools."""
        tools = [
            _make_tool("web_search"),
            _make_tool("calculator"),
            _make_tool("custom_tool"),       # NOT in priority list
            _make_tool("another_custom"),    # NOT in priority list
        ]
        result = format_for_llm(tools, compact=True)
        assert "web_search" in result
        assert "calculator" in result
        assert "custom_tool" not in result
        assert "another_custom" not in result

    def test_compact_falls_back_to_first_4_when_no_priority_tools(self):
        """When NONE of the registered tools are in the priority
        list, fall back to the first 4 so the block isn't empty/
        broken."""
        tools = [
            _make_tool("custom_a"),
            _make_tool("custom_b"),
            _make_tool("custom_c"),
            _make_tool("custom_d"),
            _make_tool("custom_e"),  # 5th — should NOT appear
        ]
        result = format_for_llm(tools, compact=True)
        assert "custom_a" in result
        assert "custom_b" in result
        assert "custom_c" in result
        assert "custom_d" in result
        assert "custom_e" not in result

    def test_compact_includes_example_for_required_args(self):
        tools = [
            _make_tool(
                "web_search",
                properties={"query": {"type": "string"}},
                required=["query"],
            ),
        ]
        result = format_for_llm(tools, compact=True)
        # Pin the exact example shape (catch any future drift)
        assert '<tool>web_search</tool><args>{"query": "..."}</args>' in result

    def test_compact_no_example_when_no_required_args(self):
        tools = [_make_tool("datetime")]
        result = format_for_llm(tools, compact=True)
        assert "datetime" in result
        assert "Example:" not in result

    def test_compact_block_structure(self):
        result = format_for_llm(
            [_make_tool("web_search")], compact=True,
        )
        # [TOOLS] open + close
        assert "[TOOLS]" in result
        assert "[/TOOLS]" in result
        # Format hint
        assert "Format: <tool>NAME</tool><args>{JSON}</args>" in result
        # Tail nudge
        assert "Only use tools when needed" in result


# ─── Full format ─────────────────────────────────────────────


class TestFullFormat:
    def test_full_includes_all_tools_no_filter(self):
        """Full format injects EVERY registered tool — capable
        cloud models can handle the longer list."""
        tools = [
            _make_tool("custom_a"),
            _make_tool("custom_b"),
            _make_tool("custom_c"),
        ]
        result = format_for_llm(tools, compact=False)
        for name in ("custom_a", "custom_b", "custom_c"):
            assert name in result

    def test_full_includes_canonical_examples(self):
        """Pin the canonical worked-example block — present in
        the full format regardless of which tools are
        registered.  Models (especially Claude) anchor on these."""
        result = format_for_llm([_make_tool("x")], compact=False)
        for canonical in (
            "web_search", "remember", "recall", "calculator", "weather",
        ):
            assert canonical in result

    def test_full_args_block_when_properties_present(self):
        tools = [
            _make_tool(
                "search",
                properties={
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            ),
        ]
        result = format_for_llm(tools, compact=False)
        assert 'Args: {"query": string, "limit": integer}' in result

    def test_full_no_args_block_when_no_properties(self):
        tools = [_make_tool("datetime")]
        result = format_for_llm(tools, compact=False)
        assert "Args:" not in result.split("Examples:")[0]


# ─── Compact / Full distinguishable ──────────────────────────


class TestCompactVsFull:
    def test_compact_default_is_false(self):
        """format_for_llm(tools) → full format (compact=False)."""
        result_default = format_for_llm([_make_tool("web_search")])
        result_full = format_for_llm(
            [_make_tool("web_search")], compact=False,
        )
        assert result_default == result_full

    def test_compact_and_full_are_different(self):
        tools = [_make_tool(
            "web_search",
            properties={"query": {"type": "string"}},
            required=["query"],
        )]
        compact = format_for_llm(tools, compact=True)
        full = format_for_llm(tools, compact=False)
        assert compact != full
        # Full has the multi-example block; compact doesn't
        assert "weather" in full
        assert "weather" not in compact


# ─── Priority-tool list pin (issue #134) ─────────────────────


class TestPriorityToolListPin:
    def test_priority_tools_constant_includes_schedule_reminder(self):
        """Pin issue #134 closure: schedule_reminder MUST be in the
        compact priority list — pre-fix local LLM hallucinated
        "no such tool available" when the user asked for a
        reminder."""
        assert "schedule_reminder" in COMPACT_PRIORITY_TOOLS

    def test_priority_tools_canonical_set(self):
        """Pin the priority-tool set so a future audit can
        compare against this list and a user-facing change to
        what local models can call has a single source of
        truth."""
        assert COMPACT_PRIORITY_TOOLS == (
            "web_search",
            "datetime",
            "remember",
            "recall",
            "calculator",
            "schedule_reminder",
        )
