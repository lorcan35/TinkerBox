"""Time-string grammar for the scheduler.

Phase 5 ε1a (refs #126, #128).  See docs/RFC-scheduler.md A1 for the
time-semantics decisions.

This module is **pure** — no asyncio, no I/O, no hidden state.  Both
the LLM-facing tool and the REST endpoint delegate here so a curl
client and a voice command get identical semantics.

Accepted grammar (RFC C.1):

  * Relative duration:    ``5m``, ``2h``, ``1h30m``, ``90s``, ``1d``
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

    raise ValueError(
        f"could not parse 'when': {s!r} — expected '5m', "
        f"ISO 8601 timestamp, or 'tomorrow at 3pm' shape"
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
