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


# ───────────────────────── verbose relative phrases (LLM-natural; #136)
#
# Local LLMs almost never emit `5m` — they reach for the conversational
# shapes a user would actually say.  These tests pin the three accepted
# verbose forms so a future regex tweak can't quietly break the LLM
# tool-call path.


def test_verbose_in_minutes() -> None:
    """`in 5 minutes` — the most common LLM phrasing for a near-term
    reminder.  Maps to the same semantics as `5m`."""
    fire_at = parse_when("in 5 minutes", now=_FIXED_NOW, tz=_UTC)
    assert fire_at == pytest.approx(_FIXED_NOW + 5 * 60)


def test_verbose_minutes_from_now() -> None:
    """`8 minutes from now` — the exact phrase that surfaced in the
    issue (#136) where ministral fired schedule_reminder and got
    rejected.  Pin it."""
    fire_at = parse_when("8 minutes from now", now=_FIXED_NOW, tz=_UTC)
    assert fire_at == pytest.approx(_FIXED_NOW + 8 * 60)


def test_verbose_seconds_later() -> None:
    """`30 seconds later` — third accepted phrasing.  `later` is a
    common English suffix; some models reach for it instead of
    `from now`."""
    fire_at = parse_when("30 seconds later", now=_FIXED_NOW, tz=_UTC)
    assert fire_at == pytest.approx(_FIXED_NOW + 30)


def test_verbose_singular_unit() -> None:
    """`in 1 hour` (singular) parses too.  English drops the trailing
    `s` for n=1 and the parser must follow."""
    fire_at = parse_when("in 1 hour", now=_FIXED_NOW, tz=_UTC)
    assert fire_at == pytest.approx(_FIXED_NOW + 3600)


def test_verbose_seconds() -> None:
    """`in 90 seconds` works; pins the second-unit path against the
    minute/hour ones."""
    fire_at = parse_when("in 90 seconds", now=_FIXED_NOW, tz=_UTC)
    assert fire_at == pytest.approx(_FIXED_NOW + 90)


def test_verbose_days() -> None:
    """`in 2 days` works; days are wall-clock 86400 s, no DST math,
    matches the relative-duration contract."""
    fire_at = parse_when("in 2 days", now=_FIXED_NOW, tz=_UTC)
    assert fire_at == pytest.approx(_FIXED_NOW + 2 * 86400)


def test_verbose_case_insensitive() -> None:
    """`In 5 Minutes` (sentence case) parses — LLM output isn't
    always lowercase."""
    fire_at = parse_when("In 5 Minutes", now=_FIXED_NOW, tz=_UTC)
    assert fire_at == pytest.approx(_FIXED_NOW + 5 * 60)


def test_verbose_far_future_capped() -> None:
    """The 365-day cap applies to verbose phrases too — a hallucinated
    `in 400 days` raises like `400d` does."""
    with pytest.raises(ValueError, match="365"):
        parse_when("in 400 days", now=_FIXED_NOW, tz=_UTC)


def test_verbose_bare_number_unit_rejected() -> None:
    """`5 minutes` (no `in` prefix and no `from now` / `later` suffix)
    is ambiguous — could mean "for 5 minutes" rather than "in 5
    minutes".  Reject so we never schedule a wrong-meaning reminder.
    The user / LLM can disambiguate by saying `in 5 minutes`."""
    with pytest.raises(ValueError):
        parse_when("5 minutes", now=_FIXED_NOW, tz=_UTC)


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
