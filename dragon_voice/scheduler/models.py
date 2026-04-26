"""Notification dataclass — the durable shape of a scheduled job.

Phase 5 ε1a (refs #126, #128).  See docs/RFC-scheduler.md Section A3
(device-scoped storage) and B.5 (SQLite schema for Tier 2) for the
field rationale.

Plain data, no behaviour — both the in-memory store (ε1a) and the
SQLite store (ε2) round-trip the same dataclass shape.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Optional


def _gen_notification_id() -> str:
    """Short hex id matching the convention from sessions._generate_session_id
    (`secrets.token_hex(6)` = 12 hex chars).  Wrapped in a `sched_` prefix
    to make grepping logs trivial (see RFC C.3 — card_id derives from this)."""
    return f"sched_{secrets.token_hex(6)}"


@dataclass
class Notification:
    """A scheduled notification job.

    Field semantics (read RFC Section A3 for storage-scope rationale):

      id              short uuid, prefixed `sched_` for log-greppability
      device_id       delivery target.  Reminders outlive sessions, so
                      this is the durable handle.
      originating_session_id
                      the session that scheduled it — UX context only
                      (NOT used for delivery target).  Will populate
                      "you set this in your conversation about X" in v2.
      fire_at         UTC epoch float.  Matches the `last_active_at REAL`
                      convention in schema.sql.
      title           ≤63 chars (widget_card.title cap)
      body            ≤255 chars (widget_card.body cap)
      tone            "info" | "warn" | "success" | "danger"
      status          "pending" | "fired" | "cancelled" | "failed"
      recurrence      reserved for Tier 2.5+; None in ε1/ε2
      created_at      UTC epoch float — when this row was inserted
      fired_at        UTC epoch float, None until status flips to fired
      cancelled_at    UTC epoch float, None until status flips to cancelled
    """

    device_id: Optional[str]
    fire_at: float
    title: str = "Reminder"
    body: str = ""
    tone: str = "info"
    originating_session_id: Optional[str] = None
    status: str = "pending"
    recurrence: Optional[str] = None
    id: str = field(default_factory=_gen_notification_id)
    created_at: float = field(default_factory=time.time)
    fired_at: Optional[float] = None
    cancelled_at: Optional[float] = None

    def __post_init__(self) -> None:
        # Truncation guards — Tab5's widget_card has hard limits and
        # Dragon shouldn't send oversized payloads expecting Tab5 to
        # silently chop them.  Trim at the dataclass boundary so all
        # downstream code (store, manager, REST response) sees the
        # post-trim shape.
        if len(self.title) > 63:
            self.title = self.title[:63]
        if len(self.body) > 255:
            self.body = self.body[:255]
        if self.tone not in ("info", "warn", "success", "danger"):
            self.tone = "info"
        if self.status not in ("pending", "fired", "cancelled", "failed"):
            self.status = "pending"


__all__ = ["Notification", "_gen_notification_id"]
