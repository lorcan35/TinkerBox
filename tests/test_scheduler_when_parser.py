"""Unit tests for ``parse_when`` — the time-string grammar.

Phase 5 ε1a (refs #126, #128).  Pure function, no asyncio.

The parser is the single source of truth for what a "when" string
means; both the LLM tool and the REST endpoint delegate to it so
"5m" and "tomorrow at 3pm" mean the same thing whether the LLM or
a curl client said them.

Coverage map (RFC Section A9):
  * relative durations (s/m/h/d, combinations like "1h30m")
  * ISO 8601 with explicit TZ
  * ISO 8601 without TZ → resolves in Dragon's local TZ
  * natural phrases ("tomorrow at 3pm", "today at 17:00")
  * malformed inputs raise ValueError
  * far-future cap at 365 days (catches LLM hallucinations)
  * past timestamps raise (you don't schedule a past reminder)
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from dragon_voice.scheduler.parser import parse_when


# ───────────────────────── fixed reference time
#
# Computed (not hardcoded) so the test stays self-consistent
# regardless of leap-second / epoch-math drift.  Reference moment:
# 2026-04-26 14:00:00 UTC.

_UTC = timezone.utc
_FIXED_NOW = datetime(2026, 4, 26, 14, 0, 0, tzinfo=_UTC).timestamp()


# ───────────────────────── relative duration grammar


def test_relative_minutes() -> None:
    """`5m` = now + 5 * 60 seconds.  Pin the unit suffix mapping."""
    fire_at = parse_when("5m", now=_FIXED_NOW, tz=_UTC)
    assert fire_at == pytest.approx(_FIXED_NOW + 5 * 60)


def test_relative_combined_units() -> None:
    """`1h30m` parses left-to-right as h + m, not as 1.5h.  Pin so a
    future regex change can't silently swap units."""
    fire_at = parse_when("1h30m", now=_FIXED_NOW, tz=_UTC)
    assert fire_at == pytest.approx(_FIXED_NOW + 60 * 60 + 30 * 60)


def test_relative_seconds_minimum() -> None:
    """`30s` works.  Useful for the ε1b live E2E ("schedule a 30s
    reminder, wait, observe widget_card")."""
    fire_at = parse_when("30s", now=_FIXED_NOW, tz=_UTC)
    assert fire_at == pytest.approx(_FIXED_NOW + 30)


def test_relative_days() -> None:
    """`2d` = 2 * 86400 s.  No DST math — relative durations are
    wall-clock seconds, not calendar days."""
    fire_at = parse_when("2d", now=_FIXED_NOW, tz=_UTC)
    assert fire_at == pytest.approx(_FIXED_NOW + 2 * 86400)


# ───────────────────────── ISO 8601


def test_iso_with_explicit_tz() -> None:
    """`2026-04-26T15:00:00-04:00` resolves to absolute UTC epoch
    regardless of Dragon's local TZ.  The tz= parameter is irrelevant
    when the input carries its own offset."""
    iso = "2026-04-26T15:00:00-04:00"
    expected_dt = datetime(2026, 4, 26, 19, 0, 0, tzinfo=_UTC)  # 15:00 EDT = 19:00 UTC
    fire_at = parse_when(iso, now=_FIXED_NOW, tz=_UTC)
    assert fire_at == pytest.approx(expected_dt.timestamp())


def test_iso_without_tz_uses_dragon_tz() -> None:
    """A bare `2026-04-26T15:00:00` (no offset) resolves in Dragon's
    local TZ.  This matches the LLM's user-facing semantics —
    "at 3pm" means "3pm where Dragon is".  Pin the policy."""
    bare_iso = "2026-04-27T15:00:00"  # well in the future from FIXED_NOW
    fire_at = parse_when(bare_iso, now=_FIXED_NOW, tz=_UTC)
    # When tz=UTC, bare 2026-04-27T15:00:00 = 1777338000.0
    expected = datetime(2026, 4, 27, 15, 0, 0, tzinfo=_UTC).timestamp()
    assert fire_at == pytest.approx(expected)


# ───────────────────────── natural phrases


def test_natural_phrase_today_at_hour() -> None:
    """`today at 17:00` resolves to 17:00 in Dragon's TZ on today's
    date (relative to `now`).  If 17:00 is already past, raises —
    "today" doesn't loop to tomorrow."""
    # FIXED_NOW = 14:00 UTC.  "today at 17:00" should resolve to 17:00 UTC same day.
    fire_at = parse_when("today at 17:00", now=_FIXED_NOW, tz=_UTC)
    expected = datetime(2026, 4, 26, 17, 0, 0, tzinfo=_UTC).timestamp()
    assert fire_at == pytest.approx(expected)


def test_natural_phrase_tomorrow_at_hour() -> None:
    """`tomorrow at 3pm` — pm form parses + jumps to next day."""
    fire_at = parse_when("tomorrow at 3pm", now=_FIXED_NOW, tz=_UTC)
    expected = datetime(2026, 4, 27, 15, 0, 0, tzinfo=_UTC).timestamp()
    assert fire_at == pytest.approx(expected)


# ───────────────────────── failure modes


def test_garbage_raises_valueerror() -> None:
    """Unparseable input → ValueError so the caller (REST or tool)
    can return a clean 400 / structured tool error."""
    with pytest.raises(ValueError):
        parse_when("not a time at all", now=_FIXED_NOW, tz=_UTC)


def test_empty_string_raises() -> None:
    with pytest.raises(ValueError):
        parse_when("", now=_FIXED_NOW, tz=_UTC)


def test_far_future_capped_at_365_days() -> None:
    """RFC E1: cap at 365 days into the future.  Anything longer is
    almost certainly a parser bug or hostile LLM hallucination."""
    with pytest.raises(ValueError, match="365"):
        parse_when("400d", now=_FIXED_NOW, tz=_UTC)


def test_past_relative_zero_seconds_allowed() -> None:
    """`0s` (= now) is technically not in the past — should parse.
    Pin so the cap-check doesn't false-fire on edge inputs."""
    fire_at = parse_when("0s", now=_FIXED_NOW, tz=_UTC)
    assert fire_at == pytest.approx(_FIXED_NOW)


def test_past_iso_raises() -> None:
    """An ISO timestamp before `now` raises.  You can't schedule a
    reminder for yesterday — that would fire immediately and confuse
    the user."""
    past_iso = "2026-04-25T12:00:00+00:00"  # 1 day before FIXED_NOW
    with pytest.raises(ValueError, match="past"):
        parse_when(past_iso, now=_FIXED_NOW, tz=_UTC)


def test_negative_relative_raises() -> None:
    """`-5m` is a pathological input — must reject, not silently
    treat as 5 minutes ago."""
    with pytest.raises(ValueError):
        parse_when("-5m", now=_FIXED_NOW, tz=_UTC)
