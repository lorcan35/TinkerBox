"""Pre-TTS text cleaner — strips markdown/punctuation that ruins
spoken-aloud flow.

User complaint (#338): "without the full stops and shit" — the LLM
emits markdown bold/italic, bullets, headings, code fences, link
syntax, parenthetical asides, and a lot of periods that the TTS
engine reads literally or pauses on for too long.  Every backend
(Piper, Kokoro, edge_tts, openrouter) is upstream of this cleaner,
so calling it from a single chokepoint in `pipeline.py` /
`text_path_tts.py` benefits all of them uniformly.

Design rules:
  * Pure function, no state, easy to unit-test.
  * Preserves the user-facing semantics — never drops a fact, only
    drops typography.  "$10" stays "$10".  "Geneva" stays "Geneva".
  * Never panics on weird input — empty / whitespace-only returns
    empty, deeply nested markdown is best-effort.
  * Sentence-level flow: collapses double-newlines to a single
    sentence boundary so a "First, ...\n\nSecond, ..." reads as
    "First, ... Second, ..." with a natural beat, not a long pause.

The cleaner is order-sensitive: code blocks must be removed before
inline-code stripping so triple-backtick fences don't get mangled
into single backticks.
"""

from __future__ import annotations

import re


# ─── Patterns (ordered by precedence) ────────────────────────────


# Triple-backtick fenced code blocks — drop entirely.  Optional
# language tag on the opening fence.  Greedy across newlines.
_RE_CODE_FENCE = re.compile(r"```[a-zA-Z0-9_\-]*\n?.*?```", re.DOTALL)

# Inline code (`like this`) — keep the content, drop the backticks.
_RE_INLINE_CODE = re.compile(r"`([^`\n]+)`")

# Markdown links `[label](url)` — keep the label, drop the URL.
_RE_MD_LINK = re.compile(r"\[([^\]]+)\]\([^\)]+\)")

# Bare URLs — drop entirely (TTS-reading "https colon slash slash"
# is brutal).  Greedy match up to whitespace.
_RE_BARE_URL = re.compile(r"https?://\S+")

# Markdown emphasis — strip the markers, keep the inner text.
# Order matters: bold (**X**, __X__) before italic (*X*, _X_) so
# the bold markers don't get half-eaten as italic pairs.
_RE_BOLD_STAR = re.compile(r"\*\*([^\*\n]+?)\*\*")
_RE_BOLD_UNDER = re.compile(r"__([^_\n]+?)__")
_RE_ITALIC_STAR = re.compile(r"(?<![\*\w])\*([^\*\n]+?)\*(?!\*)")
_RE_ITALIC_UNDER = re.compile(r"(?<![_\w])_([^_\n]+?)_(?!_)")

# Headings: `### Foo` at line start → `Foo`.  Catch 1-6 hashes.
_RE_HEADING = re.compile(r"^[ \t]*#{1,6}[ \t]+", re.MULTILINE)

# List bullets at line start: `- `, `* `, `+ `, `1. `, `2) `.
_RE_BULLET = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])\s+", re.MULTILINE)

# Block quotes: `> ` at line start.
_RE_BLOCKQUOTE = re.compile(r"^[ \t]*>[ \t]?", re.MULTILINE)

# Horizontal rules: `---`, `***`, `___` on their own line.
_RE_HR = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$", re.MULTILINE)

# Emojis + common pictographs.  Conservative byte-range strip — we
# don't want to nuke the "°" in "10°C" or the "€" sign, so this is
# narrowed to the emoji blocks proper.
#
# #338 follow-up: must also strip the variation selector (U+FE0F),
# skin-tone modifiers (U+1F3FB–U+1F3FF), and zero-width joiners
# (U+200D) that travel with emoji.  Without these the cleaner would
# leave behind orphan combining marks that TTS pronounces as "tofu"
# or skips with an audible glitch.  The trailing `[\ufe0f\u200d]*`
# and ZWJ term catch multi-codepoint emoji like "👨‍👩‍👧" too.
_RE_EMOJI = re.compile(
    "(?:"
    "[\U0001F300-\U0001F9FF"   # Misc symbols, pictographs, emoticons
    "\U0001FA70-\U0001FAFF"   # Symbols & pictographs extended-A
    "\U00002600-\U000026FF"   # Misc symbols (sun, snowflake, etc.)
    "\U00002700-\U000027BF"   # Dingbats
    "\U0001F3FB-\U0001F3FF"   # Skin-tone modifiers
    "]"
    "[\ufe0f\u200d]*"          # trailing VS16 / ZWJ joiners
    "(?:\u200d[\U0001F300-\U0001F9FF\U0001FA70-\U0001FAFF"
    "\U00002600-\U000026FF\U00002700-\U000027BF]"
    "[\ufe0f\u200d]*)*"        # ZWJ-joined sequence chain
    ")",
    flags=re.UNICODE,
)
# Belt-and-braces: any orphan VS16 / ZWJ from corrupted input.
_RE_EMOJI_ORPHAN = re.compile("[\ufe0f\u200d]+")

# Sentence flow: ". . ." or "..." → single ellipsis pause; multi
# whitespace → single space; double-newline → sentence boundary.
_RE_MULTI_DOT = re.compile(r"\.{3,}")
_RE_DOUBLE_NL = re.compile(r"\n\s*\n+")
_RE_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_RE_SOFT_NL = re.compile(r"(?<!\.)\n(?!\n)")

# Trailing ":" after a sentence — TTS reads as a hard pause that
# feels stilted in spoken replies ("Here is what I found:").  Swap
# for "—" which most TTS engines read as a natural beat.
_RE_TRAILING_COLON_LINE = re.compile(r":[ \t]*\n")


def clean_for_tts(text: str) -> str:
    """Normalize markdown-flavored LLM text for spoken-aloud TTS.

    Returns a string with markdown emphasis, headings, bullets,
    code fences, link syntax, bare URLs, emojis, and excessive
    whitespace removed/smoothed.  Empty / whitespace-only input
    returns "".

    Idempotent: running twice yields the same result as once.
    """
    if not text or not text.strip():
        return ""

    out = text

    # 1. Block-level cleanup first (so we don't leave hashes/bullets
    #    inside inline-cleaned text).
    out = _RE_CODE_FENCE.sub(" ", out)
    out = _RE_HR.sub("", out)
    out = _RE_BLOCKQUOTE.sub("", out)
    out = _RE_HEADING.sub("", out)
    out = _RE_BULLET.sub("", out)

    # 2. Inline emphasis + links + bare URLs.
    out = _RE_MD_LINK.sub(r"\1", out)
    out = _RE_BARE_URL.sub("", out)
    out = _RE_BOLD_STAR.sub(r"\1", out)
    out = _RE_BOLD_UNDER.sub(r"\1", out)
    out = _RE_ITALIC_STAR.sub(r"\1", out)
    out = _RE_ITALIC_UNDER.sub(r"\1", out)
    out = _RE_INLINE_CODE.sub(r"\1", out)

    # 3. Emojis + orphan VS16/ZWJ + ellipses + colon-newline flow.
    out = _RE_EMOJI.sub("", out)
    out = _RE_EMOJI_ORPHAN.sub("", out)
    out = _RE_MULTI_DOT.sub("…", out)
    out = _RE_TRAILING_COLON_LINE.sub(". ", out)

    # 4. Whitespace + newline normalization.  Double-newlines become
    #    a single space + period if the previous chunk didn't end in
    #    a sentence-final character (so "First, x\n\nSecond, y"
    #    becomes "First, x. Second, y" — a single beat, not a long
    #    pause).
    out = _RE_DOUBLE_NL.sub(". ", out)
    out = _RE_SOFT_NL.sub(" ", out)
    out = _RE_MULTI_SPACE.sub(" ", out)

    # 5. Clean up double-punctuation introduced by the above passes
    #    (".. " can land when the source already ended in ".\n\n").
    out = re.sub(r"\.\s*\.", ".", out)
    out = re.sub(r",\s*\.", ".", out)

    return out.strip()
