"""Unit tests for `dragon_voice.billing.spend_tracker`.

Wave 5-A of the cross-stack cohesion audit (2026-05-11).  Read-only
aggregation over the `events` table — pin both the SQL window logic
and the JSON shape parser so the rollup stays trustworthy as new
api_usage emitters land.

Run:
    python3 -m pytest tests/test_spend_tracker.py -v
"""
from __future__ import annotations

import json
import time
import unittest
from datetime import datetime, timezone

import aiosqlite
import pytest
import pytest_asyncio

from dragon_voice.billing.spend_tracker import (
    SpendSummary,
    day_iso_from_epoch,
    parse_day,
    summarize_spend_for_day,
    today_iso,
)


@pytest_asyncio.fixture
async def conn():
    """In-memory aiosqlite with the events table only."""
    c = await aiosqlite.connect(":memory:")
    await c.execute(
        """
        CREATE TABLE events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            type          TEXT NOT NULL,
            session_id    TEXT,
            device_id     TEXT,
            data          TEXT NOT NULL DEFAULT '{}',
            created_at    REAL NOT NULL
        )
        """
    )
    await c.commit()
    try:
        yield c
    finally:
        await c.close()


def _epoch_for(day: str, hour: int = 12) -> float:
    return datetime.strptime(day, "%Y-%m-%d").replace(
        tzinfo=timezone.utc, hour=hour
    ).timestamp()


async def _insert_api_usage(conn, day: str, model: str, cost_mils: int, *, hour: int = 12) -> None:
    data = json.dumps({"model": model, "cost_mils": cost_mils})
    await conn.execute(
        "INSERT INTO events (type, session_id, device_id, data, created_at) VALUES (?, ?, ?, ?, ?)",
        ("api_usage", "sess-A", "tab5-7", data, _epoch_for(day, hour)),
    )
    await conn.commit()


class ParseDayTests(unittest.TestCase):
    def test_today_iso_format(self):
        # Sanity: returns ISO YYYY-MM-DD.
        s = today_iso()
        # Will raise if not parseable
        datetime.strptime(s, "%Y-%m-%d")

    def test_parse_day_none_returns_today(self):
        self.assertEqual(parse_day(None), today_iso())
        self.assertEqual(parse_day(""), today_iso())

    def test_parse_day_valid_passes_through(self):
        self.assertEqual(parse_day("2026-05-11"), "2026-05-11")

    def test_parse_day_malformed_raises(self):
        with self.assertRaises(ValueError):
            parse_day("2026/05/11")
        with self.assertRaises(ValueError):
            parse_day("not-a-date")

    def test_day_iso_from_epoch_is_utc(self):
        # 2026-05-11 12:00 UTC → "2026-05-11"
        epoch = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(day_iso_from_epoch(epoch), "2026-05-11")


class SummarySchemaTests(unittest.TestCase):
    def test_total_cents_rounds_half_up(self):
        s = SpendSummary(day="2026-05-11", total_mils=499)
        self.assertEqual(s.total_cents, 0)
        s = SpendSummary(day="2026-05-11", total_mils=500)
        self.assertEqual(s.total_cents, 1)
        s = SpendSummary(day="2026-05-11", total_mils=12500)
        self.assertEqual(s.total_cents, 13)

    def test_to_dict_shape(self):
        s = SpendSummary(
            day="2026-05-11",
            total_mils=5500,
            event_count=3,
            by_model={"openai/gpt-4o-mini": {"mils": 5500, "count": 3}},
        )
        d = s.to_dict()
        self.assertEqual(d["day"], "2026-05-11")
        self.assertEqual(d["total_mils"], 5500)
        self.assertEqual(d["total_cents"], 6)
        self.assertEqual(d["event_count"], 3)
        self.assertEqual(d["by_model"]["openai/gpt-4o-mini"]["mils"], 5500)


class TestSummarizeSpend:
    """pytest-asyncio class — fixture-friendly.

    `Test`-prefixed so pytest picks it up by default convention; the
    other two test classes use unittest.TestCase which pytest collects
    regardless of name."""

    @pytest.mark.asyncio
    async def test_empty_table_returns_zero(self, conn):
        s = await summarize_spend_for_day(conn, day="2026-05-11")
        assert s.day == "2026-05-11"
        assert s.total_mils == 0
        assert s.event_count == 0
        assert s.by_model == {}

    @pytest.mark.asyncio
    async def test_single_row_in_window(self, conn):
        await _insert_api_usage(conn, "2026-05-11", "openai/gpt-4o-mini", 1500)
        s = await summarize_spend_for_day(conn, day="2026-05-11")
        assert s.total_mils == 1500
        assert s.event_count == 1
        assert s.by_model == {"openai/gpt-4o-mini": {"mils": 1500, "count": 1}}

    @pytest.mark.asyncio
    async def test_multiple_models_breakdown(self, conn):
        await _insert_api_usage(conn, "2026-05-11", "openai/gpt-4o-mini", 1000)
        await _insert_api_usage(conn, "2026-05-11", "openai/gpt-4o-mini", 500)
        await _insert_api_usage(conn, "2026-05-11", "anthropic/claude-haiku", 3000)
        s = await summarize_spend_for_day(conn, day="2026-05-11")
        assert s.total_mils == 4500
        assert s.event_count == 3
        assert s.by_model == {
            "openai/gpt-4o-mini": {"mils": 1500, "count": 2},
            "anthropic/claude-haiku": {"mils": 3000, "count": 1},
        }

    @pytest.mark.asyncio
    async def test_rows_outside_window_excluded(self, conn):
        # 2026-05-10: outside
        await _insert_api_usage(conn, "2026-05-10", "openai/gpt-4o-mini", 9999)
        # 2026-05-11: in
        await _insert_api_usage(conn, "2026-05-11", "openai/gpt-4o-mini", 1000)
        # 2026-05-12: outside
        await _insert_api_usage(conn, "2026-05-12", "openai/gpt-4o-mini", 9999)
        s = await summarize_spend_for_day(conn, day="2026-05-11")
        assert s.total_mils == 1000
        assert s.event_count == 1

    @pytest.mark.asyncio
    async def test_malformed_json_skipped(self, conn):
        # Insert a real row + a malformed-json row in window
        await _insert_api_usage(conn, "2026-05-11", "openai/gpt-4o-mini", 1500)
        await conn.execute(
            "INSERT INTO events (type, session_id, device_id, data, created_at) VALUES (?, ?, ?, ?, ?)",
            ("api_usage", "sess-B", "tab5-7", "{not-valid-json", _epoch_for("2026-05-11")),
        )
        await conn.commit()
        s = await summarize_spend_for_day(conn, day="2026-05-11")
        # Real row contributes; malformed row counts to event_count but not total_mils.
        assert s.total_mils == 1500
        assert s.event_count == 2

    @pytest.mark.asyncio
    async def test_non_api_usage_excluded(self, conn):
        # session.created should not be counted
        await conn.execute(
            "INSERT INTO events (type, session_id, device_id, data, created_at) VALUES (?, ?, ?, ?, ?)",
            ("session.created", "sess-A", "tab5-7", "{}", _epoch_for("2026-05-11")),
        )
        await _insert_api_usage(conn, "2026-05-11", "openai/gpt-4o-mini", 1500)
        await conn.commit()
        s = await summarize_spend_for_day(conn, day="2026-05-11")
        assert s.total_mils == 1500
        assert s.event_count == 1

    @pytest.mark.asyncio
    async def test_missing_cost_mils_treated_as_zero(self, conn):
        # Row with model but no cost_mils (e.g. local Moonshine, free)
        data = json.dumps({"model": "moonshine"})
        await conn.execute(
            "INSERT INTO events (type, session_id, device_id, data, created_at) VALUES (?, ?, ?, ?, ?)",
            ("api_usage", "sess-A", "tab5-7", data, _epoch_for("2026-05-11")),
        )
        await conn.commit()
        s = await summarize_spend_for_day(conn, day="2026-05-11")
        assert s.total_mils == 0
        assert s.event_count == 1
        assert s.by_model == {"moonshine": {"mils": 0, "count": 1}}

    @pytest.mark.asyncio
    async def test_default_day_is_today(self, conn):
        # Insert a row at "today UTC noon"
        today = today_iso()
        await _insert_api_usage(conn, today, "openai/gpt-4o-mini", 700)
        s = await summarize_spend_for_day(conn)  # day=None → today
        assert s.day == today
        assert s.total_mils == 700

    @pytest.mark.asyncio
    async def test_invalid_day_raises_value_error(self, conn):
        with pytest.raises(ValueError):
            await summarize_spend_for_day(conn, day="bogus")
