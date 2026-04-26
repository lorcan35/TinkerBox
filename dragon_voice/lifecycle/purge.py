"""Periodic retention/cleanup loops.

* :func:`periodic_purge_loop` — every 24 h, delete messages + events
  older than ``days``.  Used for message-retention policy (US-DQ14).
* :func:`media_cleanup_loop`  — every hour, delete expired media
  uploads from :class:`MediaStore`.

Both loops are designed to be cheap no-ops when the underlying service
isn't ready (shutdown races) and to swallow per-iteration exceptions so
one failure doesn't kill the loop.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def periodic_purge_loop(server: Any, days: int) -> None:
    """Run message/event purge every 24 h for ``days`` retention window."""
    while True:
        await asyncio.sleep(86400)  # 24 hours
        if server._db is None:
            break
        try:
            result = await server._db.purge_old_messages(days=days)
            logger.info(
                "Periodic purge: %d messages, %d events removed",
                result["messages"], result["events"],
            )
        except Exception as e:
            logger.warning("Periodic purge failed: %s", e)


async def media_cleanup_loop(server: Any) -> None:
    """Remove expired media uploads every hour.

    δ1 (issue #114): runs ONE cleanup pass immediately on entry
    BEFORE the first hour-long sleep.  Pre-fix the loop slept first,
    so backlogged orphans from a prior crash or a freshly-deployed
    media directory had to wait a full hour for the first sweep.
    The MediaStore has a 500 MB cap but it's only enforced lazily
    by this loop, so a busy first hour could blow through it.

    The startup pass uses the same exception swallow as the in-loop
    pass so a transient failure (e.g. SD-card mount race) still
    lets the periodic loop start.
    """
    # δ1: run cleanup once on entry before sleeping.
    try:
        await server._media_store.cleanup()
        logger.info("Startup media cleanup pass complete")
    except Exception as e:
        logger.warning("Startup media cleanup error: %s", e)

    while True:
        await asyncio.sleep(3600)
        try:
            await server._media_store.cleanup()
        except Exception as e:
            logger.warning("Media cleanup error: %s", e)
