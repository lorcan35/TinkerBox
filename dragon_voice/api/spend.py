"""Cost / spend REST routes — W5-A of the cross-stack cohesion audit
(2026-05-11).

Surfaces daily LLM spend totals from the `events` table (where
`PipelineCallbacks.on_event` persists `api_usage` rows on every
billable backend call).  Read-only — no new persistence layer, no
new schema.

## Endpoints

  GET /api/v1/spend                  → today UTC
  GET /api/v1/spend?day=YYYY-MM-DD   → specific day UTC

Response shape (see SpendSummary in spend_tracker.py):

  {
    \"day\":          \"2026-05-12\",
    \"total_mils\":   12450,
    \"total_cents\":  124,
    \"event_count\":  37,
    \"by_model\": {
      \"openai/gpt-4o-mini\":     { \"mils\": 4500,  \"count\": 12 },
      \"anthropic/claude-haiku\": { \"mils\": 7950,  \"count\": 25 }
    }
  }
"""
from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from dragon_voice.api.utils import json_error
from dragon_voice.billing.spend_tracker import summarize_spend_for_day

logger = logging.getLogger(__name__)


class SpendRoutes:
    def __init__(self, db: Any) -> None:
        self._db = db

    def register(self, app: web.Application) -> None:
        app.router.add_get("/api/v1/spend", self.get_spend)

    async def get_spend(self, request: web.Request) -> web.Response:
        """GET /api/v1/spend — daily spend summary.

        Query params:
          day=YYYY-MM-DD (UTC).  Omit for today UTC.
        """
        day = request.query.get("day")
        try:
            summary = await summarize_spend_for_day(self._db, day=day)
        except ValueError as e:
            return json_error(f"Invalid day param: {e}")
        except Exception as e:
            logger.exception("spend endpoint failed for day=%r", day)
            return json_error(f"Internal error computing spend: {e}", 500)
        return web.json_response(summary.to_dict())
