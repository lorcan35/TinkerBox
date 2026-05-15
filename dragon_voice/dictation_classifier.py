"""Dictation classifier — Dragon side of TinkerTab PR 4 action chips.

After `_post_process_dictation` lands the title + summary, this module
inspects the transcript for reminder / list cues and returns a
`{kind, confidence, payload}` hint that gets attached to the
`dictation_summary` WS frame as `proposed_action`.  Tab5 (#539) renders
an action chip below the row body when confidence ≥ 0.75.

## Design

v1 is heuristic + scheduler/parser-based — no LLM round-trip.  Two
reasons:

1. Local mode already short-circuits the LLM summary (Ollama is too slow
   for the ngrok WS idle-close window).  Adding another LLM call here
   would re-introduce the same hang class.
2. Reminder + list cues are well-served by keyword + comma-count
   heuristics; the false-positive rate of a small local LLM (ministral
   on Dragon CPU) isn't obviously lower than a tight regex.

Future work: an LLM classifier could slot in here behind a feature flag
when a fast cloud LLM is the resolved backend.

## Output shape

```python
{
    "kind": "reminder" | "list" | "none",
    "confidence": 0.0-1.0,
    "payload": {
        "when":  "ISO-8601",  # reminders only, possibly empty
        "label": "...",        # reminders only, possibly empty
    } or {}
}
```

Tab5 enforces the 0.75 floor — emitting low-confidence results is fine
(saves another roundtrip later if the threshold changes).
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from dragon_voice.scheduler.parser import parse_when

logger = logging.getLogger(__name__)


# ── Heuristic patterns ───────────────────────────────────────────────

_REMINDER_KEYWORDS = re.compile(
    r"\b(remind|reminder|call|email|text|meet|meeting|appointment|"
    r"schedule|book|pickup|pick\s?up|drop\s?off|deadline|due|"
    r"don'?t\s+forget|need\s+to|have\s+to|gotta)\b",
    re.IGNORECASE,
)

_LIST_KEYWORDS = re.compile(
    r"\b(grocer\w*|buy|shopping|todo|to-?do|list|items?|"
    r"errands?|tasks?|stuff|things)\b",
    re.IGNORECASE,
)

# Any of these in the transcript significantly bumps the reminder
# confidence (date / time anchors).
_TIME_CUE = re.compile(
    r"\b("
    r"today|tonight|tomorrow|yesterday|"
    r"mon(day)?|tue(s|sday)?|wed(nesday)?|thu(rs|rsday)?|fri(day)?|sat(urday)?|sun(day)?|"
    r"morning|afternoon|evening|night|noon|midnight|"
    r"\d{1,2}\s*(am|pm|:\d{2})|"
    r"in\s+\d+\s+(minute|hour|day|week|month)s?|"
    r"next\s+(week|month|year|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"this\s+(week|weekend|evening|afternoon|morning)"
    r")\b",
    re.IGNORECASE,
)


# ── Public API ───────────────────────────────────────────────────────


def classify_dictation(transcript: str, *, tz_name: str = "UTC") -> dict:
    """Heuristically classify a transcript for the Tab5 action chip.

    Args:
        transcript: Full dictation transcript.  May be empty.
        tz_name: IANA timezone name for natural-language date parsing.
            Defaults to UTC if the caller doesn't provide one.

    Returns:
        ``{"kind", "confidence", "payload"}`` dict.  Always returns a
        well-formed dict — never raises.

    Examples:
        >>> classify_dictation("Call mom Tuesday at 6pm")["kind"]
        'reminder'
        >>> classify_dictation("eggs, bread, butter, oat milk")["kind"]
        'list'
        >>> classify_dictation("Random thought about my day")["kind"]
        'none'
    """
    text = (transcript or "").strip()
    if not text:
        return {"kind": "none", "confidence": 0.0, "payload": {}}

    # ── Reminder scoring ────────────────────────────────────────────
    reminder_score = 0.0
    if _REMINDER_KEYWORDS.search(text):
        reminder_score += 0.40
    has_time_cue = bool(_TIME_CUE.search(text))
    if has_time_cue:
        reminder_score += 0.40

    parsed_when: Optional[str] = None
    parsed_label: Optional[str] = None
    if reminder_score > 0:
        try:
            tz = ZoneInfo(tz_name) if tz_name and tz_name != "UTC" else ZoneInfo("UTC")
        except Exception:
            tz = ZoneInfo("UTC")
        now_epoch = time.time()
        # The scheduler/parser only accepts an isolated when-phrase, not
        # full prose.  Pull out the time cue + try to resolve it.
        cue_match = _TIME_CUE.search(text)
        if cue_match:
            cue = cue_match.group(0)
            # Allow phrases like "Tuesday at 6pm" — pull a few extra words
            # of context around the match to feed the natural parser.
            ctx = _expand_time_context(text, cue_match.start(), cue_match.end())
            try:
                fire_at = parse_when(ctx, now=now_epoch, tz=tz)
                parsed_when = (
                    datetime.fromtimestamp(fire_at, tz=tz)
                    .replace(microsecond=0)
                    .isoformat(timespec="minutes")
                )
                # Tab5 strips the offset for display — keep it for
                # downstream POST /api/v1/scheduler/notifications.
                reminder_score += 0.20
            except Exception as e:
                logger.debug("classify_dictation: parse_when(%r) failed: %s", ctx, e)
        # Derive a short label from the first 8 words of the transcript.
        parsed_label = _short_label(text, max_words=8)

    # ── List scoring ────────────────────────────────────────────────
    list_score = 0.0
    if _LIST_KEYWORDS.search(text):
        list_score += 0.35
    items = [p.strip() for p in text.split(",") if p.strip()]
    comma_count = max(0, len(items) - 1)
    if comma_count >= 3:
        list_score += 0.65
    elif comma_count == 2:
        list_score += 0.40
    elif comma_count == 1:
        list_score += 0.15
    if comma_count >= 2:
        avg_words = sum(len(it.split()) for it in items) / max(1, len(items))
        if avg_words <= 3:
            list_score += 0.20

    reminder_score = min(reminder_score, 0.99)
    list_score = min(list_score, 0.99)

    # ── Pick higher-confidence option (one chip per note max) ───────
    if reminder_score >= list_score and reminder_score > 0:
        return {
            "kind": "reminder",
            "confidence": round(reminder_score, 2),
            "payload": {
                "when": parsed_when or "",
                "label": parsed_label or "",
            },
        }
    if list_score > 0:
        return {
            "kind": "list",
            "confidence": round(list_score, 2),
            "payload": {},
        }
    return {"kind": "none", "confidence": 0.0, "payload": {}}


# ── Helpers ──────────────────────────────────────────────────────────


def _expand_time_context(text: str, start: int, end: int) -> str:
    """Return the time cue with one word of context on each side.

    "I'll meet you Tuesday at 6pm." → "Tuesday at 6pm"
    Helps the natural-language parser resolve "Tuesday at 6pm" rather
    than just "Tuesday".
    """
    # Pull a small window around the match.
    left = text.rfind(" ", 0, start)
    if left < 0:
        left = 0
    right = text.find(" ", end + 8)
    if right < 0:
        right = len(text)
    chunk = text[left:right].strip()
    # Strip trailing punctuation that parse_when chokes on.
    return chunk.rstrip(".,;:!?")


def _short_label(text: str, *, max_words: int = 8) -> str:
    """First N words, lower-cased prepositions stripped from the end."""
    words = text.split()
    label = " ".join(words[:max_words]).rstrip(".,;:!?")
    return label
