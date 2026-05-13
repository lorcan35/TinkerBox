"""Background-task init step for `run_startup` — purge,
media cleanup, memory monitor.

Wave 23 SOLID-audit follow-up — twenty-eighth sub-extract
(part of the SRP-6 finalisation trio with notes_init and
mcp_init).

Owns the boot wiring for the three long-running periodic tasks
that watch over Dragon's lifetime:

  * **Message-retention purge** (US-DQ14): runs an immediate
    purge at startup to clean up sessions older than the
    configured retention window, then schedules a periodic
    re-run.  Disabled when `retention_days <= 0`.
  * **Media cleanup**: hourly removal of expired upload
    artifacts.  Always runs.
  * **Memory monitor** (audit A04): periodic RSS/temp/FD
    sampling with pipeline-drain on critical thresholds.
    Always runs.

Pre-extract this 26-LOC chunk lived inline at the tail of
`run_startup`.  Now lives in its own module.

## API

```python
init_background_tasks(server)
```

Mutates `server._purge_task`, `server._media_cleanup_task`,
`server._memory_monitor_task` — all `asyncio.Task` handles so
`run_shutdown` can cancel + await each on server stop.

## Note: synchronous entry point

This function is `def`, not `async def` — it only spawns tasks
via `asyncio.create_task`, never awaits them.  The startup
purge IS async and gets its own task too.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from dragon_voice.lifecycle import monitors, purge

logger = logging.getLogger(__name__)


def init_background_tasks(server: Any) -> None:
    """Schedule the three lifetime background tasks.

    Run LAST in the boot sequence — after all other modules are
    initialised so the tasks have everything they need.

    Side effects:
      * `server._purge_task` ← asyncio.Task running
        `purge.periodic_purge_loop` (only when retention_days>0)
      * `server._media_cleanup_task` ← asyncio.Task running
        `purge.media_cleanup_loop`
      * `server._memory_monitor_task` ← asyncio.Task running
        `monitors.memory_monitor_loop`

    The startup purge is fire-and-forget within
    `run_startup_purge_then_loop` so the boot sequence doesn't
    block on it (a slow purge on a big DB would push other
    services back).
    """
    retention_days = server._config.database.message_retention_days
    if retention_days > 0:
        # Spawn an async sub-task that does the initial purge
        # then enters the periodic loop.  This keeps the boot
        # sequence non-blocking on the initial purge.
        server._purge_task = asyncio.create_task(
            _run_startup_purge_then_loop(server, retention_days)
        )

    # Media cleanup (hourly, removes expired uploads).  Always
    # runs — there's no way to disable media uploads themselves
    # (Tab5 may send them at any time).
    server._media_cleanup_task = asyncio.create_task(
        purge.media_cleanup_loop(server)
    )

    # Start periodic memory monitor (audit A04).
    server._memory_monitor_task = asyncio.create_task(
        monitors.memory_monitor_loop(server)
    )
    rss = monitors.get_rss_mb()
    logger.info(
        "Memory monitor started (RSS=%.0f MB, warn=%d MB, crit=%d MB)",
        rss, server._mem_warn_mb, server._mem_crit_mb,
    )

    # W4-D (audit 2026-05-11): Dragon-side Tab5 coredump scraper.  Only
    # spawned when `coredump_scraper.enabled=true` in config.yaml.
    # `scraper_loop` self-no-ops when disabled, but we also gate here so
    # the task handle isn't created at all on a disabled deploy.
    server._coredump_scraper_task = None
    if getattr(server._config, "coredump_scraper", None) and \
       server._config.coredump_scraper.enabled:
        from dragon_voice import coredump_scraper as _cd
        server._coredump_scraper_task = asyncio.create_task(
            _cd.scraper_loop(server)
        )
        n = len(server._config.coredump_scraper.targets)
        logger.info(
            "W4-D coredump scraper task spawned (%d target%s, "
            "interval=%.0fs)",
            n, "" if n == 1 else "s",
            server._config.coredump_scraper.poll_interval_s,
        )


async def _run_startup_purge_then_loop(
    server: Any,
    retention_days: int,
) -> None:
    """Run the initial message purge, then enter the periodic
    purge loop.  Wraps both so a single asyncio.Task handle
    covers both phases.

    The startup purge is wrapped in try/except so a transient
    DB issue doesn't tear down the whole task — the periodic
    loop still gets to run.
    """
    try:
        result = await server._db.purge_old_messages(days=retention_days)
        logger.info(
            "Startup purge complete: %d messages, %d events removed "
            "(retention=%d days)",
            result["messages"], result["events"], retention_days,
        )
    except Exception as e:
        logger.warning("Startup purge failed: %s", e)

    await purge.periodic_purge_loop(server, retention_days)
