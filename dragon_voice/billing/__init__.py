"""Billing — cost-tracking + daily-cap support.

Wave 5 of the cross-stack cohesion audit (2026-05-11).  Audit found
no aggregated cost-tracking surface: `_PRICING_MILS_PER_M` exists in
the OpenRouter backend, every per-turn cost lands in the `events`
table via `PipelineCallbacks.on_event` (W4-C also stamped them with
`turn_id`), but there's no \"what did I spend today\" query.

This package is read-only aggregation over the existing events table
plus a REST surface.  Daily-cap-trigger + outbound `cap_downgrade`
emit is W5-B follow-up.
"""
from dragon_voice.billing.spend_tracker import (
    SpendSummary,
    day_iso_from_epoch,
    parse_day,
    today_iso,
    summarize_spend_for_day,
)

__all__ = [
    "SpendSummary",
    "day_iso_from_epoch",
    "parse_day",
    "today_iso",
    "summarize_spend_for_day",
]
