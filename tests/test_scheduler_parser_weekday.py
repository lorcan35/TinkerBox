"""Tests for the #330 weekday + time-of-day parser shapes.

Sits alongside test_scheduler_when_parser.py but focuses only on the
new grammar branches added for TinkerTab PR 4 reminder chips.

Reference moment: 2026-05-19 14:00:00 UTC.  That's a Tuesday — pin so
weekday-resolution tests have predictable today/tomorrow weekdays.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from dragon_voice.scheduler.parser import parse_when

_UTC = timezone.utc
# Tuesday 2026-05-19 14:00 UTC — keeps the math intuitive (Mon=0..Sun=6;
# today.weekday() == 1).
_NOW = datetime(2026, 5, 19, 14, 0, 0, tzinfo=_UTC).timestamp()


# ── Weekday + time ───────────────────────────────────────────────────


def test_weekday_future_today() -> None:
    """\"Tuesday at 6 PM\" said on Tuesday at 14:00 → same day 18:00."""
    fire_at = parse_when("Tuesday at 6 PM", now=_NOW, tz=_UTC)
    expected = datetime(2026, 5, 19, 18, 0, 0, tzinfo=_UTC).timestamp()
    assert fire_at == pytest.approx(expected)


def test_weekday_past_today_rolls_to_next_week() -> None:
    """\"Tuesday at 9 AM\" said on Tuesday at 14:00 — 9 AM already
    passed today, so schedule next Tuesday."""
    fire_at = parse_when("Tuesday at 9 AM", now=_NOW, tz=_UTC)
    expected = datetime(2026, 5, 26, 9, 0, 0, tzinfo=_UTC).timestamp()
    assert fire_at == pytest.approx(expected)


def test_weekday_other_day() -> None:
    """\"Friday at 14:30\" said Tue → that coming Friday."""
    fire_at = parse_when("Friday at 14:30", now=_NOW, tz=_UTC)
    expected = datetime(2026, 5, 22, 14, 30, 0, tzinfo=_UTC).timestamp()
    assert fire_at == pytest.approx(expected)


def test_weekday_short_form() -> None:
    """\"wed at 9am\" — 3-letter abbreviation parses the same as
    \"wednesday\"."""
    fire_at = parse_when("wed at 9am", now=_NOW, tz=_UTC)
    expected = datetime(2026, 5, 20, 9, 0, 0, tzinfo=_UTC).timestamp()
    assert fire_at == pytest.approx(expected)


def test_next_weekday_forces_next_week() -> None:
    """\"next Tuesday at 6 PM\" said on Tuesday afternoon → +7 days,
    not same day."""
    fire_at = parse_when("next Tuesday at 6 PM", now=_NOW, tz=_UTC)
    expected = datetime(2026, 5, 26, 18, 0, 0, tzinfo=_UTC).timestamp()
    assert fire_at == pytest.approx(expected)


def test_weekday_24h_clock() -> None:
    """\"Saturday at 21:00\" — 24h clock with no AM/PM."""
    fire_at = parse_when("Saturday at 21:00", now=_NOW, tz=_UTC)
    expected = datetime(2026, 5, 23, 21, 0, 0, tzinfo=_UTC).timestamp()
    assert fire_at == pytest.approx(expected)


# ── Time-of-day aliases ──────────────────────────────────────────────


def test_tomorrow_morning() -> None:
    """\"tomorrow morning\" → tomorrow 09:00."""
    fire_at = parse_when("tomorrow morning", now=_NOW, tz=_UTC)
    expected = datetime(2026, 5, 20, 9, 0, 0, tzinfo=_UTC).timestamp()
    assert fire_at == pytest.approx(expected)


def test_tomorrow_afternoon() -> None:
    """\"tomorrow afternoon\" → tomorrow 13:00."""
    fire_at = parse_when("tomorrow afternoon", now=_NOW, tz=_UTC)
    expected = datetime(2026, 5, 20, 13, 0, 0, tzinfo=_UTC).timestamp()
    assert fire_at == pytest.approx(expected)


def test_tomorrow_evening() -> None:
    """\"tomorrow evening\" → tomorrow 19:00."""
    fire_at = parse_when("tomorrow evening", now=_NOW, tz=_UTC)
    expected = datetime(2026, 5, 20, 19, 0, 0, tzinfo=_UTC).timestamp()
    assert fire_at == pytest.approx(expected)


def test_tonight() -> None:
    """\"tonight\" said at 14:00 → today 19:00."""
    fire_at = parse_when("tonight", now=_NOW, tz=_UTC)
    expected = datetime(2026, 5, 19, 19, 0, 0, tzinfo=_UTC).timestamp()
    assert fire_at == pytest.approx(expected)


def test_bare_today_rejected() -> None:
    """\"today\" alone is too ambiguous — falls through to ValueError."""
    with pytest.raises(ValueError):
        parse_when("today", now=_NOW, tz=_UTC)


def test_tonight_past_rejected() -> None:
    """\"tonight\" said at 22:00 — 19:00 already passed; reject as past."""
    late = datetime(2026, 5, 19, 22, 0, 0, tzinfo=_UTC).timestamp()
    with pytest.raises(ValueError):
        parse_when("tonight", now=late, tz=_UTC)
