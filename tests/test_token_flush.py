"""Tests for ``dragon_voice.token_flush``.

Pin the constants + helper truth tables.  The existing
test_tts_flush_and_kill tests cover the same ground via
re-exported names from pipeline.py — this file pins the
canonical module-level API.
"""
from __future__ import annotations

from dragon_voice.token_flush import (
    CLAUSE_END,
    HALLUCINATION_STOPS,
    LAST_WORD_BOUNDARY,
    LOCAL_TIMEOUT_FLUSH_MIN_CHARS,
    LOCAL_TIMEOUT_FLUSH_S,
    SENTENCE_END,
    SENTENCE_SPLIT,
    TRIPLE_BACKTICK,
    pick_timeout_flush_index,
    update_code_block_state,
)


# ─── Constants ──────────────────────────────────────────────


class TestConstants:
    def test_local_timeout_flush_s_is_300ms(self):
        """Phase 2 H2 (#94) tuned value.  Lower → over-fires;
        higher → user feels the gap."""
        assert LOCAL_TIMEOUT_FLUSH_S == 0.30

    def test_local_timeout_flush_min_chars_is_20(self):
        """Lower → micro-stutters every word."""
        assert LOCAL_TIMEOUT_FLUSH_MIN_CHARS == 20

    def test_triple_backtick_is_canonical_markdown(self):
        assert TRIPLE_BACKTICK == "```"


# ─── SENTENCE_END / CLAUSE_END regexes ──────────────────────


class TestSentenceEnd:
    def test_period_matches(self):
        assert SENTENCE_END.search("Hello world.")

    def test_question_mark_matches(self):
        assert SENTENCE_END.search("Are you sure?")

    def test_exclamation_matches(self):
        assert SENTENCE_END.search("Wait!")

    def test_trailing_whitespace_tolerated(self):
        assert SENTENCE_END.search("End.   ")

    def test_no_terminator_no_match(self):
        assert not SENTENCE_END.search("Mid sentence")


class TestClauseEnd:
    def test_comma_matches(self):
        assert CLAUSE_END.search("first clause,")

    def test_semicolon_matches(self):
        assert CLAUSE_END.search("clause one;")

    def test_colon_matches(self):
        assert CLAUSE_END.search("here:")

    def test_em_dash_matches(self):
        assert CLAUSE_END.search("aside—")

    def test_period_does_not_match(self):
        """Period is sentence-end, not clause-end."""
        assert not CLAUSE_END.search("done.")


# ─── SENTENCE_SPLIT ──────────────────────────────────────────


class TestSentenceSplit:
    def test_splits_at_punctuation_plus_whitespace(self):
        text = "First sentence. Second sentence! Third?"
        parts = SENTENCE_SPLIT.split(text)
        assert parts == ["First sentence.", "Second sentence!", "Third?"]

    def test_no_split_without_whitespace(self):
        text = "abbrev.example"  # no space after period
        parts = SENTENCE_SPLIT.split(text)
        assert parts == [text]


# ─── LAST_WORD_BOUNDARY ──────────────────────────────────────


class TestLastWordBoundary:
    def test_finds_last_space_position(self):
        s = "Let me search for that information"
        m = LAST_WORD_BOUNDARY.search(s)
        assert m is not None
        # Match starts at the space before "information"
        assert s[m.start()] == " "
        assert s[: m.start()] == "Let me search for that"

    def test_single_word_no_match(self):
        assert LAST_WORD_BOUNDARY.search("supercalifragilistic") is None


# ─── pick_timeout_flush_index helper ────────────────────────


class TestPickTimeoutFlushIndex:
    def test_returns_last_whitespace_position(self):
        assert pick_timeout_flush_index("Hello world here") == len("Hello world")

    def test_returns_none_for_single_word(self):
        assert pick_timeout_flush_index("noWhitespace") is None

    def test_returns_none_for_empty(self):
        assert pick_timeout_flush_index("") is None


# ─── update_code_block_state helper ─────────────────────────


class TestUpdateCodeBlockState:
    def test_opening_backticks_toggles_in(self):
        assert update_code_block_state(False, "```python\n") is True

    def test_closing_backticks_toggles_out(self):
        assert update_code_block_state(True, "```\n") is False

    def test_no_backticks_preserves_state(self):
        assert update_code_block_state(True, "def foo():") is True
        assert update_code_block_state(False, "plain text") is False

    def test_inline_code_pair_no_toggle(self):
        """Inline ` ```code``` ` pair = even count → no net
        toggle."""
        assert update_code_block_state(False, "use ```code``` here") is False
        assert update_code_block_state(True, "still ```code``` in code") is True


# ─── HALLUCINATION_STOPS ────────────────────────────────────


class TestHallucinationStops:
    def test_user_marker_matches(self):
        assert HALLUCINATION_STOPS.search("answer\nUser: next question")

    def test_human_marker_matches(self):
        assert HALLUCINATION_STOPS.search("answer\nHuman: more")

    def test_assistant_marker_matches(self):
        assert HALLUCINATION_STOPS.search("\nAssistant: continuation")

    def test_im_end_marker_matches(self):
        # Pattern requires (?:^|\n\n\n|\n) prefix
        assert HALLUCINATION_STOPS.search("answer\n<|im_end|>")

    def test_case_insensitive(self):
        assert HALLUCINATION_STOPS.search("answer\nUSER: next")

    def test_word_user_in_prose_does_not_match(self):
        """User: with no preceding newline/start shouldn't fire."""
        # Match requires (?:^|\n\n\n|\n) prefix
        assert not HALLUCINATION_STOPS.search("the user said hi")
