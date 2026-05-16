"""#338: pre-TTS text cleaner — strips markdown / bullets / code /
emojis / bare URLs so the TTS backend doesn't read literal
punctuation aloud.

The user complaint that motivated this: TC mode replies came back
with `**bold**`, bullet lists, `### Headings`, code fences, and
trailing colons — all read literally or with awkward pauses.
"""

from __future__ import annotations

from dragon_voice.tts.text_cleaner import clean_for_tts


# ─── Trivial inputs ────────────────────────────────────────────────


def test_empty_returns_empty():
    assert clean_for_tts("") == ""


def test_whitespace_only_returns_empty():
    assert clean_for_tts("   \n\n\t  ") == ""


def test_plain_prose_unchanged_modulo_whitespace():
    assert clean_for_tts("Hello there.") == "Hello there."


# ─── Bold + italic emphasis ────────────────────────────────────────


def test_strip_bold_star():
    assert clean_for_tts("This is **bold** text.") == "This is bold text."


def test_strip_bold_underscore():
    assert clean_for_tts("Try __this__ now.") == "Try this now."


def test_strip_italic_star():
    assert clean_for_tts("He said *yes*.") == "He said yes."


def test_strip_italic_underscore():
    assert clean_for_tts("Use _care_ here.") == "Use care here."


def test_strip_multiple_bold_in_one_sentence():
    # Colon stays mid-sentence (no newline immediately after it) so the
    # cleaner only flattens the bold markers.
    assert (
        clean_for_tts("**Geneva**: weather is **10°C**, partly cloudy.")
        == "Geneva: weather is 10°C, partly cloudy."
    )


# ─── Headings + bullets + blockquotes ──────────────────────────────


def test_strip_heading():
    assert clean_for_tts("### Current Weather\nIt is sunny.") == "Current Weather It is sunny."


def test_strip_h1_through_h6():
    for n in range(1, 7):
        text = "#" * n + " Title here"
        assert clean_for_tts(text) == "Title here"


def test_strip_dash_bullets():
    src = "- First item\n- Second item\n- Third item"
    out = clean_for_tts(src)
    assert "-" not in out
    assert "First item" in out and "Third item" in out


def test_strip_numbered_bullets():
    src = "1. First\n2. Second\n3) Third"
    out = clean_for_tts(src)
    assert "First" in out and "Second" in out and "Third" in out
    # Numbers preserved only if they're not list markers
    # (the leading "1. " markers are stripped)
    assert "1. First" not in out


def test_strip_blockquote():
    assert clean_for_tts("> wisdom here") == "wisdom here"


# ─── Code fences + inline code ─────────────────────────────────────


def test_drop_fenced_code_block():
    src = "Here is some code:\n```python\nprint('hi')\n```\nDone."
    out = clean_for_tts(src)
    assert "print" not in out
    assert "Done." in out


def test_inline_code_keeps_content_drops_backticks():
    assert clean_for_tts("Run `npm install` to setup.") == "Run npm install to setup."


# ─── Links + bare URLs ─────────────────────────────────────────────


def test_md_link_keeps_label_drops_url():
    src = "See [the docs](https://example.com/docs) for more."
    out = clean_for_tts(src)
    assert "the docs" in out
    assert "https" not in out
    assert "example.com" not in out


def test_bare_url_dropped():
    src = "Reference: https://wikipedia.org/wiki/Geneva for details."
    out = clean_for_tts(src)
    assert "https" not in out
    assert "wikipedia.org" not in out
    assert "Reference" in out and "details" in out


# ─── Emojis + ellipses ─────────────────────────────────────────────


def test_strip_common_emojis():
    src = "Geneva is 🌤️ partly cloudy and 10°C."
    out = clean_for_tts(src)
    assert "🌤️" not in out
    assert "10°C" in out  # the degree sign is NOT an emoji — preserve it


def test_collapse_triple_dots_to_ellipsis():
    assert clean_for_tts("Wait....") == "Wait…"


def test_horizontal_rule_dropped():
    assert clean_for_tts("First section.\n---\nSecond section.") in (
        "First section. Second section.",
        "First section. Second section",
    )


# ─── Sentence flow ─────────────────────────────────────────────────


def test_double_newline_becomes_sentence_boundary():
    src = "First sentence\n\nSecond sentence"
    assert clean_for_tts(src) == "First sentence. Second sentence"


def test_trailing_colon_newline_becomes_period():
    src = "Here's the plan:\nDo the thing."
    out = clean_for_tts(src)
    assert ":" not in out
    assert "plan" in out and "Do the thing" in out


def test_multiple_spaces_collapsed():
    assert clean_for_tts("hello    world") == "hello world"


# ─── Composite real-world examples ─────────────────────────────────


def test_realworld_tinkerclaw_reply():
    """The actual narration form captured live from PR #335
    verification — bold, bullets, emoji, colon-newline, all in one."""
    src = (
        "**Skill being loaded:** `weather` — the skill for fetching weather information.\n\n"
        "**SKILL.md location:** `~/.tinkerclaw/skills/weather/SKILL.md`\n\n"
        "Now fetching Geneva weather:\n"
        "- **Temp:** +10°C\n"
        "- **Feels like:** +10°C\n"
        "- **Wind:** ↓5km/h"
    )
    out = clean_for_tts(src)
    # No markdown markers
    assert "**" not in out
    assert "`" not in out
    assert "###" not in out
    # No bullet dashes
    assert "- Temp" not in out
    # Content preserved
    assert "Skill being loaded" in out
    assert "weather" in out
    assert "10°C" in out
    assert "Wind" in out
    # No trailing colon hanging
    assert ":\n" not in out
    assert "weather:" not in out  # colon-newline rewritten to period


def test_realworld_geneva_weekend_reply():
    src = (
        "## Weekend in Geneva\n\n"
        "Two morning options:\n"
        "1. **Musée d'Art** — Geneva's largest art museum (https://mah-geneve.ch).\n"
        "2. **Musée d'Histoire Naturelle** — Switzerland's biggest natural history museum.\n\n"
        "For lunch, try `Café du Centre` in the old town."
    )
    out = clean_for_tts(src)
    assert "## " not in out
    assert "**" not in out
    assert "`" not in out
    assert "https" not in out
    assert "Geneva" in out
    assert "Musée d'Art" in out
    assert "Café du Centre" in out


def test_idempotent():
    """Running the cleaner twice yields the same result as once."""
    src = "**Hello**\n\n- bullet 1\n- bullet 2\n\n`code` and [link](https://x.com)."
    once = clean_for_tts(src)
    twice = clean_for_tts(once)
    assert once == twice


def test_preserves_currency_and_units():
    """User-visible facts (numbers, currency, units) must survive."""
    src = "**Pricing:** $19.99 for 500ml at 18°C — that's €15.50."
    out = clean_for_tts(src)
    assert "$19.99" in out
    assert "500ml" in out
    assert "18°C" in out
    assert "€15.50" in out
