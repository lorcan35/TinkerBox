"""Scheduler module — async push surface for Dragon.

Phase 5 of the UX-gap remediation.  See docs/RFC-scheduler.md for
the full architecture (split across ε1a / ε1b / ε2 PRs).

Re-exports the public surface so callers don't need to know which
submodule a symbol lives in.
"""
from dragon_voice.scheduler.manager import (
    RUNAWAY_CAP_PER_DEVICE,
    RunawayCapError,
    SchedulerManager,
)
from dragon_voice.scheduler.models import Notification
from dragon_voice.scheduler.parser import parse_when
from dragon_voice.scheduler.store import (
    InMemoryNotificationStore,
    NotificationStore,
)

__all__ = [
    "Notification",
    "NotificationStore",
    "InMemoryNotificationStore",
    "SchedulerManager",
    "RunawayCapError",
    "RUNAWAY_CAP_PER_DEVICE",
    "parse_when",
]
