"""Token-flush helpers + constants for the LLM streaming loop.

Wave 23 SOLID-audit follow-up — thirty-eighth sub-extract.
Fifth slice from `dragon_voice/pipeline.py` (audit SRP-4).

The voice path's `_process_utterance` streams LLM tokens
through a buffer that's flushed to TTS when:

  1. **Sentence end** (.!?) — natural sentence boundary.
  2. **Clause end** (,;: — past 20 chars) — start TTS earlier
     on slow models so the user hears something within the
     first second of the reply.
  3. **Timeout flush** (300 ms with 20+ chars buffered) —
     handles the LLM rambling without punctuation case.
  4. **Word-boundary split** for the timeout flush — never
     chop a word in half.

A code-block-aware toggle suppresses the clause + timeout
flushes inside ``` ... ``` blocks so `def foo():` doesn't
trigger a structural-colon flush.

Pre-extract these were 6 module-level regexes/constants on
`pipeline.py` plus the toggle-tracking logic inline in the
streaming loop.  Now lives as free helpers + constants.

## API

```python
# Constants (all module-level)
_SENTENCE_END        # ".", "!", "?" at end of buffer
_CLAUSE_END          # ",;:—" at end of buffer
_TRIPLE_BACKTICK     # "```" — code-block delimiter
_LAST_WORD_BOUNDARY  # finds the last whitespace
_LOCAL_TIMEOUT_FLUSH_S        # 0.30s — silence-then-flush window
_LOCAL_TIMEOUT_FLUSH_MIN_CHARS  # 20 chars — minimum buffer
_HALLUCINATION_STOPS  # truncate at "User:"/"Human:"/etc.

# Helpers
in_code = update_code_block_state(in_code, token)
flush_idx = pick_timeout_flush_index(buffer)
```

## Phase 2 H2 (#94) constants pinned

The 300 ms / 20-char defaults are the empirically-tuned
values from issue #94.  Lowering MIN_CHARS micro-stutters
every word; lowering TIMEOUT_FLUSH_S over-fires.  Pinned by
test_tts_flush_and_kill (existing) + new
test_token_flush_helpers (this PR).
"""
from __future__ import annotations

import re
from typing import Optional


# ── Sentence + clause boundary regexes ───────────────────────

# `.!?` at end of buffer (with optional trailing whitespace).
SENTENCE_END = re.compile(r"[.!?]\s*$")

# Splitter for completed sentences (used to break a bigger
# buffer into per-sentence chunks downstream).
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

# Clause boundary: `,;:` and em-dash `—` (U+2014).  Used in
# local mode to start TTS earlier on slow models so the user
# hears something within the first second.  The character
# class duplicates the em-dash as a Unicode escape AND a
# literal — preserved verbatim from pre-extract behaviour
# (cosmetic; both compile to the same set).
CLAUSE_END = re.compile(r"[,;:——]\s*$")


# ── Word-boundary timeout-flush helper ───────────────────────

# Finds the LAST whitespace position in the buffer so the
# timeout flush splits at a word break, not mid-word.
LAST_WORD_BOUNDARY = re.compile(r"\s\S*$")


# ── Code-block detection ─────────────────────────────────────

# While inside a code block (`` ``` ... ``` ``) the clause-flush is
# suppressed (a colon in `def foo():` is structural, not a natural
# pause).  The sentence-flush (.!?) still applies because periods
# are rare in code blocks and a `.` is usually meaningful (e.g.
# `obj.method()`).  Timeout-flush also suppressed in code so the
# whole block emits as one TTS unit.
TRIPLE_BACKTICK = "```"


# ── Phase 2 H2 (#94) timeout-flush parameters ───────────────

# 300 ms is long enough that a normal punctuation-rich response
# never trips it (sentences land their `.` well within 300 ms of
# each other on any model > 5 tok/s) but short enough that an
# LLM rambling without punctuation still feels responsive.
LOCAL_TIMEOUT_FLUSH_S = 0.30

# Minimum buffer of 20 chars prevents micro-stuttered chunks.
LOCAL_TIMEOUT_FLUSH_MIN_CHARS = 20


# ── Hallucination stop patterns ──────────────────────────────

# LLMs sometimes simulate user turns or continue generating
# after answering.  Truncate response at these markers so the
# user doesn't hear the model fabricating their next prompt.
HALLUCINATION_STOPS = re.compile(
    r"(?:^|\n\n\n|\n)(User:|Human:|Assistant:|<\|end|<\|im_end)",
    re.IGNORECASE,
)


# ── Helper: code-block toggle ────────────────────────────────


def update_code_block_state(in_code_block: bool, token: str) -> bool:
    """Update the code-block-membership flag based on a new
    streaming token.  Toggles when the token contains an ODD
    number of triple-backtick markers (an inline ` ```code``` `
    pair has even count → no toggle).

    Pin: pre-extract this lived inline in the streaming loop.
    The "odd count toggles" rule means `here is ```python\\n`
    flips to in_code, then `def foo():\\n` keeps it in_code,
    then ` ``` end` flips it back.
    """
    if TRIPLE_BACKTICK in token:
        n_marks = token.count(TRIPLE_BACKTICK)
        if n_marks % 2 == 1:
            return not in_code_block
    return in_code_block


# ── Helper: timeout-flush split index ────────────────────────


def pick_timeout_flush_index(buffer: str) -> Optional[int]:
    """For the timeout flush, find the index of the last
    whitespace in `buffer` so the flush splits at a word break.

    Returns the index of the whitespace character, or None when
    no whitespace exists (single-word buffer — caller should
    hold the whole thing for the next iteration to avoid
    chopping the word).

    Pre-extract this was inlined as
    `m = _LAST_WORD_BOUNDARY.search(sentence_buffer)` —
    extracted as a helper so the call site reads like its
    intent + tests can pin the no-whitespace edge case.
    """
    m = LAST_WORD_BOUNDARY.search(buffer)
    if m is None:
        return None
    return m.start()
