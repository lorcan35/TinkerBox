"""Pair-emission helper for the progress event bus (β-arch).

Phase 6 of the UX-gap remediation (issue #123, refs #89, #94).

Migrated emitters in pipeline.py and server.py double-write — they
send BOTH the legacy ad-hoc event AND the new ``progress`` event so
unmodified Tab5 firmware in production keeps working unchanged.
This helper packages the double-write into a single one-line call
so emit sites stay readable and the (ConnectionError, RuntimeError)
swallow is consistent across all of them.

The swallow pattern matches the existing convention at
``pipeline.py:1007/1017/1041`` — a torn-down WS mid-emit must NOT
take down the calling task.  Worst case: Tab5 sees only the legacy
frame (status quo) or only the progress frame (consistent with the
``progress_bus_emit_legacy=False`` mode).  Both states are valid.

W5 S3-8 audit (2026-05-30): the ``progress`` half of the dual-write
has NO consumers fleet-wide at present — Tab5 firmware logs it as
"Unknown message type: progress" (voice_ws_proto.c) and the dashboard
(dashboard.py + dragon_voice/static/) never reads it.  It is NOT dead
code to delete, though: it is the *intentional forward-compat* frame
the β-arch added so a future progress-bus consumer (dashboard live
view, a new client) gets a structured event without re-touching every
emit site.  The legacy half is load-bearing for Tab5 today and is kept
unconditionally.  Removal of the progress half is deferred until either
a real consumer ships (then it's load-bearing too) or the progress-bus
is abandoned (then drop the half + flip emit_legacy semantics).  Do NOT
collapse the dual-write on the strength of "no consumers" alone.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from dragon_voice.errors import Scope, Severity
from dragon_voice.progress import Phase, Stage, progress_event

logger = logging.getLogger(__name__)


# Type alias for the per-connection emit callback wired by VoicePipeline /
# VoiceServer (an async function that takes a dict and sends it).
OnEvent = Callable[[dict], Awaitable[None]]


async def emit_progress_pair(
    on_event: OnEvent,
    *,
    legacy: Optional[dict],
    phase: Phase,
    stage: Stage,
    payload: Optional[dict] = None,
    code: Optional[str] = None,
    message: Optional[str] = None,
    severity: Optional[Severity] = None,
    scope: Optional[Scope] = None,
    emit_legacy: bool = True,
) -> None:
    """Send the legacy event + the new progress event in that order.

    Parameters
    ----------
    on_event:
        The per-connection async emit callback.  Already closes over
        the WS handle, so we don't need to know about it here.
    legacy:
        The legacy ad-hoc event dict to send first.  ``None`` when
        the new event is purely additive (no historical equivalent
        exists — relevant for future phases that didn't have an
        ad-hoc form).
    phase, stage:
        Forwarded to :func:`dragon_voice.progress.progress_event`.
    payload, code, message, severity, scope:
        Forwarded to :func:`dragon_voice.progress.progress_event`.
    emit_legacy:
        When False, skip the legacy frame entirely (post-Tab5-update
        cleanup mode).  Defaults True per the transition strategy.

    Notes
    -----
    Both sends are individually wrapped in
    ``try / except (ConnectionError, RuntimeError)`` so a torn-down
    WS mid-pair doesn't propagate.  Each failure logs at DEBUG so
    ops can see drops without flooding the journal.
    """
    if emit_legacy and legacy is not None:
        try:
            await on_event(legacy)
        except (ConnectionError, RuntimeError) as e:
            logger.debug(
                "progress-bus legacy frame drop (phase=%s, stage=%s): %s",
                phase.value, stage.value, e,
            )

    try:
        await on_event(progress_event(
            phase=phase,
            stage=stage,
            payload=payload,
            code=code,
            message=message,
            severity=severity,
            scope=scope,
        ))
    except (ConnectionError, RuntimeError) as e:
        logger.debug(
            "progress-bus new frame drop (phase=%s, stage=%s): %s",
            phase.value, stage.value, e,
        )


__all__ = ["emit_progress_pair", "OnEvent"]
