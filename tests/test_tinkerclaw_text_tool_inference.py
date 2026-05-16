"""#334: assert that agent narration in TC streams surfaces tool_call
events even though the upstream gateway never emits OpenAI tool_calls
deltas.

The unit under test is `extract_inferred_tool_invocations` plus the
private `_scan_text_for_inferred_tools` method which feeds the existing
W7-A callback pipeline.
"""

from __future__ import annotations

from dragon_voice.llm.tinkerclaw_llm import (
    extract_inferred_tool_invocations,
    _normalize_tool_name,
)


# ── _normalize_tool_name ──────────────────────────────────────────────


def test_normalize_lowercase_strip():
    assert _normalize_tool_name("Weather") == "weather"
    assert _normalize_tool_name("  Browser  ") == "browser"


def test_normalize_spaces_to_underscores():
    assert _normalize_tool_name("Stock Ticker") == "stock_ticker"
    assert _normalize_tool_name("Web Search") == "web_search"


def test_normalize_date_aliases_to_datetime():
    assert _normalize_tool_name("date") == "datetime"
    assert _normalize_tool_name("Date") == "datetime"


# ── extract_inferred_tool_invocations: positives ──────────────────────


def test_pattern_based_on_my_x_skill_fires_for_known_tool():
    # The actual live-captured agent narration from #334's reproducer
    text = (
        "Let me try the correct path:\n\n"
        "Based on my weather skill, let me fetch live data for Geneva:"
    )
    assert extract_inferred_tool_invocations(text) == ["weather"]


def test_pattern_using_the_tool_form():
    text = "Using the browser tool to open the page now."
    assert extract_inferred_tool_invocations(text) == ["browser"]


def test_pattern_let_me_use_my_skill():
    text = "Let me use my memory skill to recall what we discussed."
    assert extract_inferred_tool_invocations(text) == ["memory"]


def test_verb_subject_pattern_searching_the_web():
    text = "Searching the web for the latest currency rates now."
    assert extract_inferred_tool_invocations(text) == ["web"]


def test_verb_subject_pattern_fetching_weather():
    text = "Fetching weather data for Geneva right now."
    assert extract_inferred_tool_invocations(text) == ["weather"]


def test_verb_subject_pattern_querying_memory():
    text = "Querying memory for previous conversations about coffee."
    assert extract_inferred_tool_invocations(text) == ["memory"]


def test_multi_word_subject_stock_ticker_normalised():
    text = "Fetching stock ticker data for $AAPL."
    assert extract_inferred_tool_invocations(text) == ["stock_ticker"]


def test_dedupe_within_single_pass():
    # The same tool mentioned twice in one text should only emit once
    text = (
        "Based on my weather skill, let me fetch live data. "
        "Using the weather skill again to confirm."
    )
    assert extract_inferred_tool_invocations(text) == ["weather"]


def test_already_fired_set_skips_repeat():
    text = "Based on my weather skill, let me fetch live data."
    assert extract_inferred_tool_invocations(
        text, already_fired={"weather"},
    ) == []


def test_two_distinct_tools_fire_in_order():
    text = (
        "Using my memory skill to recall context, then "
        "searching the web for fresh data."
    )
    result = extract_inferred_tool_invocations(text)
    assert "memory" in result and "web" in result
    assert result.index("memory") < result.index("web")


# ── extract_inferred_tool_invocations: negatives (false positives) ────


def test_no_match_on_based_on_my_knowledge():
    # "knowledge" is not in _TC_KNOWN_TOOLS — allowlist gates this out
    text = "Based on my knowledge of the topic, I think the answer is yes."
    assert extract_inferred_tool_invocations(text) == []


def test_no_match_on_based_on_my_understanding():
    text = "Based on my understanding, the system uses redis."
    assert extract_inferred_tool_invocations(text) == []


def test_no_match_on_let_me_try_a_different_approach():
    # CoT preamble already handled by _COT_PREAMBLE_PATTERNS — must not
    # spuriously fire here
    text = "Let me try a different approach to this problem."
    assert extract_inferred_tool_invocations(text) == []


def test_no_match_on_using_the_keyboard():
    # "keyboard" not in allowlist
    text = "Using the keyboard you can type messages quickly."
    assert extract_inferred_tool_invocations(text) == []


def test_no_match_on_empty_text():
    assert extract_inferred_tool_invocations("") == []
    assert extract_inferred_tool_invocations(None) == []  # type: ignore[arg-type]


def test_no_match_on_plain_prose():
    text = (
        "Geneva is a city in Switzerland. The weather there is usually "
        "mild in summer and cold in winter."
    )
    assert extract_inferred_tool_invocations(text) == []


# ── #334 regex broadening: MiniMax-style narration variants ───────────


def test_pattern_found_the_x_skill():
    """MiniMax narration form captured live: 'Found the weather skill'."""
    text = (
        "Here's the breakdown:\n"
        "1. **Found the weather skill** — let me use it."
    )
    assert extract_inferred_tool_invocations(text) == ["weather"]


def test_pattern_ran_the_x_tool():
    text = 'Ran the web_search tool with query "weather Geneva".'
    assert extract_inferred_tool_invocations(text) == ["web_search"]


def test_pattern_invoked_the_x_skill():
    text = "Invoked the memory skill to recall prior context."
    assert extract_inferred_tool_invocations(text) == ["memory"]


def test_pattern_called_the_x_tool():
    text = "Called the browser tool to open the page."
    assert extract_inferred_tool_invocations(text) == ["browser"]


def test_pattern_skill_path_reference():
    """Skill-path references in agent narration are high-precision."""
    text = "Located at `~/.tinkerclaw/skills/weather/SKILL.md` per docs."
    assert extract_inferred_tool_invocations(text) == ["weather"]


def test_pattern_openclaw_path_form():
    text = "See openclaw/skills/memory/SKILL.md for the schema."
    assert extract_inferred_tool_invocations(text) == ["memory"]


def test_skill_md_filename_only():
    """`skills/web_search/SKILL.md` alone is enough — high precision."""
    text = "Per skills/web_search/SKILL.md the entry is `search`."
    assert extract_inferred_tool_invocations(text) == ["web_search"]
