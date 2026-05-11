"""Spend tracker — read-only aggregation over `events` table.

Wave 5-A of the cross-stack cohesion audit (2026-05-11).  Surfaces
\"what did I spend today\" without introducing a new persistence
layer — every per-turn cost is already written to the events table
as `type='api_usage'` rows by `PipelineCallbacks.on_event` (since
TT #328 Wave 12 + tightened with W4-C's turn_id stamp).

## Schema assumption

Each `api_usage` event row has:
  - `type` = `\"api_usage\"`
  - `created_at` = epoch seconds (REAL)
  - `data` = JSON string with at least:
      - `model` — backend model id (e.g. `\"openai/gpt-4o-mini\"`)
      - `cost_mils` — integer cost in mils (1 mil = 1/1000 cent)
  - Optional extras: `tokens`, `prompt_tokens`, `completion_tokens`,
    `turn_id` (W4-C), session_id / device_id (top-level columns)

Rows with no `model` field or `cost_mils=0` are still counted in
`event_count` (they're real events) but contribute 0 to `total_mils`
and aren't broken out in `by_model`.

## API

  >>> summary = await summarize_spend_for_day(db, day=\"2026-05-11\")
  >>> summary.total_mils
  12450
  >>> summary.by_model
  {\"openai/gpt-4o-mini\": {\"mils\": 4500, \"count\": 12}, ...}

Day boundaries are UTC.  Pass `day=None` for today UTC.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import aiosqlite

logger = logging.getLogger(__name__)


@dataclass
class SpendSummary:
    """One day's spend rolled up.  Serialises straight to JSON via
    `.to_dict()`."""

    day: str                          # ISO YYYY-MM-DD (UTC)
    total_mils: int = 0               # sum of cost_mils across the day
    event_count: int = 0              # total api_usage rows (including cost_mils=0)
    by_model: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def total_cents(self) -> int:
        # Round-half-up at the cent boundary — chats expect a whole-cent number.
        return (self.total_mils + 500) // 1000

    def to_dict(self) -> dict:
        return {
            "day":         self.day,
            "total_mils":  self.total_mils,
            "total_cents": self.total_cents,
            "event_count": self.event_count,
            "by_model":    self.by_model,
        }


def today_iso() -> str:
    """Current day in UTC, ISO YYYY-MM-DD."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def day_iso_from_epoch(epoch_seconds: float) -> str:
    """Convert epoch seconds → UTC ISO YYYY-MM-DD."""
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime("%Y-%m-%d")


def parse_day(day: Optional[str]) -> str:
    """Validate + normalise an ISO YYYY-MM-DD string.  Returns
    today UTC when `day` is None or empty.  Raises ValueError on
    malformed input."""
    if not day:
        return today_iso()
    # strict YYYY-MM-DD; strptime catches typos like "2026/5/11"
    parsed = datetime.strptime(day, "%Y-%m-%d")
    return parsed.strftime("%Y-%m-%d")


def _day_bounds_epoch(day_iso: str) -> tuple[float, float]:
    """UTC midnight..next-midnight epoch seconds for the given day."""
    start = datetime.strptime(day_iso, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start_epoch = start.timestamp()
    return (start_epoch, start_epoch + 86400.0)


async def summarize_spend_for_day(
    conn_or_db: Any,
    day: Optional[str] = None,
) -> SpendSummary:
    """Aggregate api_usage rows for the given day into a SpendSummary.

    Args:
        conn_or_db: either an aiosqlite.Connection (preferred — what
            tests use with `:memory:` fixtures) OR a `Database`
            instance with a `.conn` attribute (what the production
            API handler passes — see `dragon_voice/db.py`).
        day: ISO YYYY-MM-DD UTC, or None for today UTC.

    Returns:
        SpendSummary with totals + per-model breakdown.

    Notes:
        - Rows with malformed JSON in `data` are skipped + counted in
          `event_count` (we don't want one bad row to mask the day's
          real spend).
        - Cost_mils is read as int; non-numeric values count as 0.
    """
    normalised_day = parse_day(day)
    start_epoch, end_epoch = _day_bounds_epoch(normalised_day)
    summary = SpendSummary(day=normalised_day)

    rows = await _fetch_api_usage_rows(conn_or_db, start_epoch, end_epoch)
    for raw_data in rows:
        summary.event_count += 1
        try:
            data = json.loads(raw_data) if raw_data else {}
        except (json.JSONDecodeError, TypeError):
            logger.debug("spend_tracker: skipped malformed api_usage row")
            continue
        if not isinstance(data, dict):
            continue
        cost = data.get("cost_mils") or 0
        try:
            cost_int = int(cost)
        except (TypeError, ValueError):
            cost_int = 0
        summary.total_mils += cost_int
        model = data.get("model")
        if model:
            entry = summary.by_model.setdefault(model, {"mils": 0, "count": 0})
            entry["mils"] += cost_int
            entry["count"] += 1

    return summary


async def _fetch_api_usage_rows(
    conn_or_db: Any, start_epoch: float, end_epoch: float
) -> list[str]:
    """Run the SELECT against either a Database wrapper or a raw
    aiosqlite.Connection.  Returns the list of `data` column values
    (JSON strings)."""
    sql = """
        SELECT data FROM events
        WHERE type = 'api_usage'
          AND created_at >= ?
          AND created_at <  ?
    """
    params = (start_epoch, end_epoch)

    # Path 1: a Database wrapper (db.conn is the aiosqlite.Connection).
    conn = getattr(conn_or_db, "conn", None)
    if conn is not None:
        cursor = await conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

    # Path 2: raw aiosqlite.Connection (test fixture friendly).
    if isinstance(conn_or_db, aiosqlite.Connection):
        cursor = await conn_or_db.execute(sql, params)
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

    raise TypeError(
        f"spend_tracker: unsupported db type {type(conn_or_db).__name__} — "
        "expected Database with .conn or aiosqlite.Connection"
    )
