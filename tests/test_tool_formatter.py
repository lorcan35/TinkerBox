"""Tests for ``dragon_voice.tools.formatter``.

Pin every formatter branch + the priority-tool list (issue #134
closure) so a future refactor can't silently regress to losing
schedule_reminder from the compact format.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from dragon_voice.tools.formatter import (
    COMPACT_PRIORITY_THRESHOLD,
    COMPACT_PRIORITY_TOOLS,
    _is_compact_eligible,
    format_for_llm,
)


def _make_tool(
    name: str,
    *,
    description: str = "test desc",
    properties: dict | None = None,
    required: list[str] | None = None,
    priority: int = 50,
) -> MagicMock:
    tool = MagicMock()
    tool.name = name
    tool.description = description
    tool.parameters_schema = {
        "properties": properties or {},
        "required": required or [],
    }
    tool.priority = priority
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


# ─── OCP-3 priority-attribute path (PR #251) ─────────────────


class TestPriorityAttributeOcp3:
    def test_default_priority_is_50_excluded_from_compact(self):
        """Default-priority tools (not in legacy name list) must
        NOT appear in compact — preserves "noisy tool list confuses
        small models" semantic."""
        tools = [_make_tool("custom_tool", priority=50)]
        result = format_for_llm(tools, compact=True)
        # Falls back to first 4 since none are priority — but
        # "custom_tool" IS the only registered tool, so it appears
        # via fallback.  Construct the test such that a default-50
        # tool doesn't qualify on the priority path while priority
        # tools that DO qualify are also present.
        priority_tool = _make_tool("web_search", priority=50)  # name-list match
        default_tool = _make_tool("custom_tool", priority=50)  # neither path
        result = format_for_llm(
            [priority_tool, default_tool], compact=True,
        )
        assert "web_search" in result
        # custom_tool is excluded because its priority is 50 AND
        # its name is not in the legacy list
        assert "custom_tool" not in result

    def test_low_priority_attribute_includes_in_compact(self):
        """OCP-3 closure: a tool with priority < threshold must be
        included in the compact prompt EVEN IF its name is not in
        COMPACT_PRIORITY_TOOLS — lets new tools opt into the
        compact slot without editing the formatter."""
        new_tool = _make_tool("brand_new_tool", priority=15)
        result = format_for_llm([new_tool], compact=True)
        assert "brand_new_tool" in result

    def test_high_priority_attribute_excluded_from_compact(self):
        """priority >= threshold means "not in compact" — pin the
        boundary semantic."""
        tools = [
            _make_tool("legacy_priority", priority=10),  # in
            _make_tool("at_threshold", priority=50),     # out (not strict <)
            _make_tool("over_threshold", priority=99),   # out
        ]
        # Need at least ONE legacy or low-priority tool to avoid
        # the fallback-to-first-N path.
        result = format_for_llm(tools, compact=True)
        assert "legacy_priority" in result
        assert "at_threshold" not in result
        assert "over_threshold" not in result

    def test_legacy_name_list_path_still_works(self):
        """Backward compat pin: a tool with default priority=50
        but a name in the legacy list MUST still appear (existing
        Tool subclasses don't set priority)."""
        legacy_tool = _make_tool("calculator", priority=50)
        result = format_for_llm([legacy_tool], compact=True)
        assert "calculator" in result

    def test_both_paths_or_combined(self):
        """A tool qualifies via EITHER path (OR-combined) so legacy
        + new tools can coexist in the same registry."""
        legacy = _make_tool("web_search", priority=50)        # name match
        new = _make_tool("smart_search", priority=20)         # priority match
        result = format_for_llm([legacy, new], compact=True)
        assert "web_search" in result
        assert "smart_search" in result


# ─── _is_compact_eligible helper ─────────────────────────────


class TestCompactEligibility:
    def test_legacy_name_match_eligible(self):
        tool = _make_tool("web_search", priority=50)
        assert _is_compact_eligible(tool) is True

    def test_low_priority_eligible(self):
        tool = _make_tool("anything", priority=10)
        assert _is_compact_eligible(tool) is True

    def test_default_priority_non_legacy_name_not_eligible(self):
        tool = _make_tool("randomtool", priority=50)
        assert _is_compact_eligible(tool) is False

    def test_threshold_constant_pin(self):
        """Pin: threshold is 50.  Lower = higher priority."""
        assert COMPACT_PRIORITY_THRESHOLD == 50

    def test_tool_without_priority_attr_treated_as_50(self):
        """getattr fallback: an external Tool subclass that doesn't
        inherit from Tool (custom impl) must still work — assume
        priority=50 (excluded)."""
        bare = MagicMock(spec=["name"])
        bare.name = "external"
        # No priority attribute at all
        assert _is_compact_eligible(bare) is False
