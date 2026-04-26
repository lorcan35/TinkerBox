"""Tests for response_wrap.looks_like_useful_text.

Audit C5 (#142): the empty-reply guard heuristic was previously a
private helper in server.py.  The voice path used a strict-empty check
(`not text.strip()`) which missed the bracket-noise-only failure mode
exhibited by FC-trained models (xLAM emits residual `<` after the
parser strips the well-formed `<tool>...</tool>` block; gemma3:4b
G2-math emitted a single `<`).  Moving the heuristic to
`tools/response_wrap` lets both paths use it; this file is the
behaviour-pinning test for the heuristic itself.
"""
from __future__ import annotations

import pytest

from dragon_voice.tools.response_wrap import looks_like_useful_text


@pytest.mark.parametrize(
    "text",
    [
        "Hi there",
        "Got it.",
        "456 * 789 = 359784",
        "OK!",  # 3 chars after stripping bang+whitespace = 2; but 'OK!' has 3 meaningful
        "It's 3pm in Tokyo.",
        "Sure — done.",
    ],
)
def test_useful_text_returns_true(text: str) -> None:
    assert looks_like_useful_text(text) is True, f"expected useful: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "",        # genuinely empty
        "   ",     # whitespace only
        "<",       # the gemma3:4b G2 stray bracket
        "[]",      # bracket noise
        "{}",      # brace noise
        "  <>  ",  # whitespace + bracket
        "<tool>store_fact</tool>",   # closing-tag fingerprint -> junk
        "[tool]remember</tool><args>{}</args>",  # xLAM residual leak
        "<args>",  # opening tag without close, but well-formed; stripped to empty
        "ab",      # 2 chars: below the 3-char threshold
    ],
)
def test_junk_text_returns_false(text: str) -> None:
    assert looks_like_useful_text(text) is False, f"expected junk: {text!r}"


def test_closing_tag_fingerprint_short_circuits() -> None:
    """Even when content surrounds the closing tag, the fingerprint
    treats the whole string as junk -- this is the FC-residual case
    where the LLM never produced a real reply, just stripped tool
    markup.  Failing 'open' on this would re-introduce the empty-
    bubble bug the wrap was built to fix."""
    assert looks_like_useful_text("hello </tool> world") is False


def test_bracket_blocks_stripped_before_length_check() -> None:
    """Well-formed `<…>`, `[…]`, `{…}` content is removed before the
    3-char count.  A reply that's 100 chars of stripped XML but only
    2 chars of natural text is not useful."""
    assert looks_like_useful_text("ab <some long block of stuff>") is False


def test_unicode_meaningful_chars_count() -> None:
    """Non-ASCII letters are still meaningful.  xLAM occasionally
    emits non-ASCII tool names; we don't want the heuristic to
    discount real localised replies."""
    assert looks_like_useful_text("こんにちは") is True
