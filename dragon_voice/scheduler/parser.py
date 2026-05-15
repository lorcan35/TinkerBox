"""Time-string grammar for the scheduler.

Phase 5 ε1a (refs #126, #128).  See docs/RFC-scheduler.md A1 for the
time-semantics decisions.

This module is **pure** — no asyncio, no I/O, no hidden state.  Both
the LLM-facing tool and the REST endpoint delegate here so a curl
client and a voice command get identical semantics.

Accepted grammar (RFC C.1):

  * Relative duration:    ``5m``, ``2h``, ``1h30m``, ``90s``, ``1d``
  * Verbose relative:     ``in 5 minutes``, ``8 minutes from now``,
                          ``30 seconds later`` (#136 — LLMs rarely emit
                          the compact ``5m`` form; pin the conversational
                          shapes the tool gets in practice)
  * ISO 8601 absolute:    ``2026-04-26T15:00:00-04:00`` (with TZ)
                          ``2026-04-27T15:00:00`` (resolves in tz=)
  * Natural phrase:       ``today at 17:00``, ``tomorrow at 3pm``

All resolve to a UTC epoch float (``time.time()`` shape).  Reject:
  * Empty / unparseable input  → ValueError
  * Past timestamps (ISO/natural)  → ValueError("past")
  * Negative relative durations  → ValueError
  * Anything > 365 days in the future  → ValueError("365")
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, tzinfo
from typing import Optional


# ───────────────────────── grammars

# Relative duration: "5m", "1h30m", "90s", "1d2h".  Components in any
# order; each unit appears 0 or 1 times.  Whole-number values only —
# we don't support "1.5h" because it invites rounding bugs and the
# user can write "90m" instead.
_DURATION_PART = re.compile(r"(\d+)([smhd])")
_DURATION_FULLMATCH = re.compile(r"^(?:\d+[smhd])+$")
_UNIT_TO_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# Natural phrase: "today at 17:00", "tomorrow at 3pm", "today at 3:30pm"
_NATURAL = re.compile(
    r"^\s*(today|tomorrow)\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$",
    re.IGNORECASE,
)

# #330: weekday + time, "Tuesday at 6 PM", "next monday at 9am", "wed at 14:30".
# Three-letter abbreviations + full names both accepted.  "next" prefix
# forces at least 7 days out (so "next monday" on a Monday means a week
# from today, not today).
_WEEKDAY_NATURAL = re.compile(
    r"^\s*(?P<next>next\s+)?"
    r"(?P<wday>mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:rs|rsday)?|"
    r"fri(?:day)?|sat(?:urday)?|sun(?:day)?)"
    r"\s+at\s+(?P<hour>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<ampm>am|pm)?\s*$",
    re.IGNORECASE,
)
_WEEKDAY_INDEX = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

# #330: time-of-day aliases.  "tomorrow morning" → 09:00, afternoon → 13:00,
# evening → 19:00.  "tonight" is a synonym for "today evening".
_TOD_NATURAL = re.compile(
    r"^\s*(?P<day>today|tomorrow|tonight)"
    r"(?:\s+(?P<tod>morning|afternoon|evening|night))?\s*$",
    re.IGNORECASE,
)
_TOD_DEFAULT_HOUR = {
    "morning": 9,
    "afternoon": 13,
    "evening": 19,
    "night": 21,
}

# Verbose relative phrasing the LLM emits in practice (#136).  Either an
# `in N <unit>` prefix OR an `N <unit> from now` / `N <unit> later`
# suffix is required — bare `5 minutes` is intentionally rejected
# because it's ambiguous between "in 5 minutes" and "for 5 minutes".
_VERBOSE_PREFIX = re.compile(
    r"^\s*in\s+(\d+)\s+(second|minute|hour|day)s?\s*$",
    re.IGNORECASE,
)
_VERBOSE_SUFFIX = re.compile(
    r"^\s*(\d+)\s+(second|minute|hour|day)s?\s+(?:from\s+now|later)\s*$",
    re.IGNORECASE,
)
_VERBOSE_UNIT_TO_SECONDS = {
    "second": 1, "minute": 60, "hour": 3600, "day": 86400,
}

# 365-day far-future cap (RFC E1)
_MAX_FUTURE_SECONDS = 365 * 86400


def parse_when(
    s: str,
    *,
    now: float,
    tz: tzinfo,
) -> float:
    """Parse a "when" string into a UTC epoch float.

    Parameters
    ----------
    s:
        The user / LLM input.  See module docstring for grammar.
    now:
        Current UTC epoch (``time.time()``).  Passed in as a parameter
        instead of reading clock here — keeps the function pure +
        testable with a fixed reference time.
    tz:
        Dragon's local timezone (``datetime.timezone(...)`` or a
        ``zoneinfo.ZoneInfo``).  Used to resolve bare ISO timestamps
        (no offset) and natural phrases.  Caller passes whatever
        ``time.localtime()`` reports — the LLM tool and REST endpoint
        each construct this once.

    Returns
    -------
    float
        UTC epoch timestamp at which the notification should fire.

    Raises
    ------
    ValueError
        On unparseable input, past timestamps, negative durations,
        or far-future inputs (> 365 days).
    """
    if not isinstance(s, str) or not s.strip():
        raise ValueError("empty when string")

    s_clean = s.strip()

    # ── 1. Relative duration ────────────────────────────────────────
    fire_at = _try_parse_relative(s_clean, now=now)
    if fire_at is not None:
        _check_far_future(fire_at, now=now)
        return fire_at

    # ── 1b. Verbose relative ("in 5 minutes", "8 minutes from now") ──
    fire_at = _try_parse_verbose_relative(s_clean, now=now)
    if fire_at is not None:
        _check_far_future(fire_at, now=now)
        return fire_at

    # ── 2. ISO 8601 ─────────────────────────────────────────────────
    fire_at = _try_parse_iso(s_clean, tz=tz)
    if fire_at is not None:
        _check_past(fire_at, now=now)
        _check_far_future(fire_at, now=now)
        return fire_at

    # ── 3. Natural phrase ───────────────────────────────────────────
    fire_at = _try_parse_natural(s_clean, now=now, tz=tz)
    if fire_at is not None:
        _check_past(fire_at, now=now)
        _check_far_future(fire_at, now=now)
        return fire_at

    # ── 3b. Weekday + time, "Tuesday at 6 PM" (#330) ────────────────
    fire_at = _try_parse_weekday(s_clean, now=now, tz=tz)
    if fire_at is not None:
        _check_past(fire_at, now=now)
        _check_far_future(fire_at, now=now)
        return fire_at

    # ── 3c. Time-of-day alias, "tomorrow morning" / "tonight" (#330) ─
    fire_at = _try_parse_time_of_day(s_clean, now=now, tz=tz)
    if fire_at is not None:
        _check_past(fire_at, now=now)
        _check_far_future(fire_at, now=now)
        return fire_at

    raise ValueError(
        f"could not parse 'when': {s!r} — expected '5m', "
        f"'in 5 minutes', ISO 8601 timestamp, 'tomorrow at 3pm', "
        f"'Tuesday at 6 PM', or 'tomorrow morning' shape"
    )


# ───────────────────────── helpers


def _try_parse_relative(s: str, *, now: float) -> Optional[float]:
    """Parse "5m" / "1h30m" / etc. or return None if not the shape."""
    if not _DURATION_FULLMATCH.match(s):
        # Negative durations like "-5m" intentionally don't match the
        # full-string anchor — fall through and let the natural-phrase
        # branch reject them with a generic error.  Pin the negative
        # rejection here too so the error message is specific:
        if s.startswith("-"):
            raise ValueError(f"negative duration not allowed: {s!r}")
        return None

    total_seconds = 0
    for value, unit in _DURATION_PART.findall(s):
        total_seconds += int(value) * _UNIT_TO_SECONDS[unit]
    return now + total_seconds


def _try_parse_verbose_relative(s: str, *, now: float) -> Optional[float]:
    """Parse `in 5 minutes` / `8 minutes from now` / `30 seconds later`.

    Returns None if the input doesn't match one of the verbose shapes
    so the caller can fall through to the next grammar branch.
    """
    m = _VERBOSE_PREFIX.match(s) or _VERBOSE_SUFFIX.match(s)
    if not m:
        return None
    value = int(m.group(1))
    unit = m.group(2).lower()
    return now + value * _VERBOSE_UNIT_TO_SECONDS[unit]


def _try_parse_iso(s: str, *, tz: tzinfo) -> Optional[float]:
    """Parse a full ISO 8601 string.  Returns None if it doesn't look
    like ISO — the caller falls through to the natural-phrase branch."""
    # Cheap shape check before invoking fromisoformat — saves a
    # ValueError throw/catch on every relative-duration call that
    # already failed the relative grammar.
    if "T" not in s or len(s) < 10:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # Bare timestamp, no offset → resolve in Dragon's local TZ.
        dt = dt.replace(tzinfo=tz)
    return dt.timestamp()


def _try_parse_natural(s: str, *, now: float, tz: tzinfo) -> Optional[float]:
    """Parse "today at 3pm" / "tomorrow at 17:00" / etc."""
    m = _NATURAL.match(s)
    if not m:
        return None

    day_word = m.group(1).lower()
    hour = int(m.group(2))
    minute = int(m.group(3) or "0")
    ampm = (m.group(4) or "").lower()

    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0

    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        raise ValueError(f"invalid hour/minute in natural phrase: {s!r}")

    # "today" = today's date in `tz`; "tomorrow" = +1 day.
    today_local = datetime.fromtimestamp(now, tz=tz).date()
    target_date = today_local
    if day_word == "tomorrow":
        target_date = today_local + timedelta(days=1)

    target_dt = datetime(
        target_date.year, target_date.month, target_date.day,
        hour, minute, 0, tzinfo=tz,
    )
    return target_dt.timestamp()


def _resolve_clock(hour: int, minute: int, ampm: str, *, src: str) -> tuple[int, int]:
    """Normalise (hour, minute, ampm) → (hour_24, minute) with bounds check."""
    ampm = (ampm or "").lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        raise ValueError(f"invalid hour/minute in natural phrase: {src!r}")
    return hour, minute


def _try_parse_weekday(s: str, *, now: float, tz: tzinfo) -> Optional[float]:
    """Parse "Tuesday at 6 PM" / "next monday at 9am" / "wed at 14:30".

    Resolution rule: target weekday + time → next future occurrence.
    "next <weekday>" adds 7 days when the bare resolution would land
    today or earlier this week, so it's always at least a week out.
    """
    m = _WEEKDAY_NATURAL.match(s)
    if not m:
        return None
    next_prefix = bool(m.group("next"))
    wday_word = m.group("wday").lower()
    target_wday = _WEEKDAY_INDEX[wday_word]
    hour, minute = _resolve_clock(
        int(m.group("hour")), int(m.group("min") or "0"),
        m.group("ampm") or "", src=s,
    )

    today_local = datetime.fromtimestamp(now, tz=tz)
    today_wday = today_local.weekday()  # Mon=0..Sun=6
    days_ahead = (target_wday - today_wday) % 7
    # If it's the same weekday AND the requested time is already past
    # today, push to next week.  ("Monday at 6 PM" said at 8 PM Monday
    # means a week from now.)
    if days_ahead == 0:
        candidate = today_local.replace(
            hour=hour, minute=minute, second=0, microsecond=0,
        )
        if candidate.timestamp() <= now:
            days_ahead = 7
    if next_prefix and days_ahead < 7:
        days_ahead += 7
    target_date = today_local.date() + timedelta(days=days_ahead)
    target_dt = datetime(
        target_date.year, target_date.month, target_date.day,
        hour, minute, 0, tzinfo=tz,
    )
    return target_dt.timestamp()


def _try_parse_time_of_day(s: str, *, now: float, tz: tzinfo) -> Optional[float]:
    """Parse "tomorrow morning" / "tonight" / "today evening".

    Default hours: morning=09:00, afternoon=13:00, evening=19:00,
    night=21:00.  "tonight" is shorthand for "today evening".  Bare
    "today" / "tomorrow" without a TOD alias is intentionally rejected
    here so the caller falls through to a more explicit error than
    "today" alone could carry.
    """
    m = _TOD_NATURAL.match(s)
    if not m:
        return None
    day_word = m.group("day").lower()
    tod_word = (m.group("tod") or "").lower()
    # "tonight" implies evening even without a TOD suffix.
    if day_word == "tonight":
        target_hour = _TOD_DEFAULT_HOUR["evening"]
        day_offset = 0
    else:
        if not tod_word:
            return None  # "today" / "tomorrow" alone — too ambiguous
        target_hour = _TOD_DEFAULT_HOUR[tod_word]
        day_offset = 1 if day_word == "tomorrow" else 0
    today_local = datetime.fromtimestamp(now, tz=tz).date()
    target_date = today_local + timedelta(days=day_offset)
    target_dt = datetime(
        target_date.year, target_date.month, target_date.day,
        target_hour, 0, 0, tzinfo=tz,
    )
    return target_dt.timestamp()


def _check_past(fire_at: float, *, now: float) -> None:
    """Reject ISO/natural timestamps that resolve to the past.  Don't
    apply to relative durations (those are computed from `now` so
    can't naturally land in the past unless the caller passes negative
    seconds, which `_try_parse_relative` already rejects)."""
    if fire_at < now:
        raise ValueError(
            f"refusing to schedule reminder in the past "
            f"(fire_at={fire_at}, now={now})"
        )


def _check_far_future(fire_at: float, *, now: float) -> None:
    """RFC E1: cap at 365 days into the future.  Anything longer is
    almost certainly a parser bug or hostile LLM hallucination."""
    if fire_at - now > _MAX_FUTURE_SECONDS:
        raise ValueError(
            f"refusing to schedule reminder more than 365 days "
            f"in the future (would fire at {fire_at}, now={now})"
        )


__all__ = ["parse_when"]
