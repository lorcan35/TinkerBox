"""Server drain sequence — ``run_shutdown(server, app)``.

Run in strict order so aiohttp can exit cleanly:

1.  Cancel + await the three periodic tasks (memory monitor, purge,
    media cleanup).  Await is critical — cancel-without-await left the
    tasks attached long enough for aiohttp to close the loop first,
    producing ``RuntimeError: Event loop is closed`` at systemctl
    restart (W14-M09, W13-H3).
2.  Shut down per-connection pipelines (gather, return_exceptions=True
    so one failure doesn't block the rest).
3.  Shut down the shared backend pool (W15-C01 — backends are only
    torn down here; per-pipeline shutdowns are no-ops for pooled
    backends).
4.  Shut down the shared inference ``ThreadPoolExecutor`` so uvloop can
    exit.
5.  Close shared HTTP clients (proxy session for dashboard, media
    pipeline's ClientSession — W14-H12).
6.  Shut down notes, memory service (W14-H11), conversation, session
    manager, DB — each guarded by a presence check.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)


async def run_shutdown(server: Any, app: web.Application) -> None:
    """Clean up all active sessions and foundation modules on shutdown."""
    logger.info(
        "Server shutting down — closing %d connections",
        len(server._active_connections),
    )

    # Cancel periodic tasks.  Await after cancel so we don't leak the
    # task across loop shutdown (W14-M09).
    if server._memory_monitor_task and not server._memory_monitor_task.done():
        server._memory_monitor_task.cancel()
        try:
            await server._memory_monitor_task
        except (asyncio.CancelledError, Exception):
            pass
    if server._purge_task and not server._purge_task.done():
        # periodic_purge_loop sleeps 86400 s in one shot; cancel-without-
        # await left it attached long enough for aiohttp to close the
        # loop first, producing "RuntimeError: Event loop is closed".
        server._purge_task.cancel()
        try:
            await server._purge_task
        except (asyncio.CancelledError, Exception):
            pass
    # Wave 13 H3: the media cleanup loop was being left running on
    # shutdown because it wasn't touched here.  If it was mid-sleep when
    # the loop closes, asyncio logs "Task was destroyed but it is
    # pending" warnings and the 24 h TTL cleaner can orphan partial
    # deletes.  Cancel + await.
    if server._media_cleanup_task and not server._media_cleanup_task.done():
        server._media_cleanup_task.cancel()
        try:
            await server._media_cleanup_task
        except (asyncio.CancelledError, Exception):
            pass

    # Phase 5 ε1a (issue #128): cancel scheduler tasks BEFORE pipeline
    # drain so an in-flight notification fire doesn't race with a
    # closed WS.  Pattern matches the cancel-then-await discipline
    # above (W14-M09 fix).
    if getattr(server, "_scheduler_mgr", None):
        try:
            await server._scheduler_mgr.shutdown()
        except Exception:
            logger.debug("SchedulerManager shutdown raised", exc_info=True)

    # Drain per-connection pipelines
    tasks = []
    for _ws_id, conn in list(server._active_connections.items()):
        pipeline = conn.get("pipeline")
        if pipeline:
            tasks.append(pipeline.shutdown())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    server._active_connections.clear()

    # W15-C01: now that no pipelines are holding references, shut down
    # the shared backend pool.  Per-pipeline shutdowns above are no-ops
    # for pooled backends (_pooled_* flag), so the pool is the single
    # owner responsible for final teardown.
    pool_tasks = []
    for key, backend in list(server._backend_pool.items()):
        logger.info("W15-C01: releasing pooled backend %s", key)
        try:
            pool_tasks.append(backend.shutdown())
        except (AttributeError, RuntimeError) as e:
            logger.warning("W15-C01: pool backend %s shutdown raised: %s", key, e)
    if pool_tasks:
        await asyncio.gather(*pool_tasks, return_exceptions=True)
    server._backend_pool.clear()

    # v4·D audit P0 fix: release the inference ThreadPoolExecutor so
    # uvloop can exit cleanly.  Previously zombie threads lingered.
    try:
        from dragon_voice.pipeline import shutdown_inference_executor
        shutdown_inference_executor(wait=False)
        logger.info("Inference executor shut down")
    except Exception:
        logger.debug("inference executor shutdown failed", exc_info=True)

    # Close shared proxy session (DQ08)
    if server._proxy_session and not server._proxy_session.closed:
        await server._proxy_session.close()

    # Shut down foundation
    if server._notes_svc:
        await server._notes_svc.shutdown()
    if server._media_pipeline:
        # W14-H12: close the MediaPipeline's shared aiohttp
        # ClientSession.  Prior behaviour left it dangling across
        # systemctl restart, logging "Unclosed client session" and
        # making the wave-13 FD counter noisy.
        try:
            await server._media_pipeline.close()
        except Exception:
            logger.debug("MediaPipeline shutdown failed", exc_info=True)
    if server._memory_service:
        logger.info("Shutting down memory service")
        # W14-H11: close the shared Ollama HTTP session.
        try:
            await server._memory_service.shutdown()
        except Exception:
            logger.debug("MemoryService shutdown raised", exc_info=True)
        server._memory_service = None
    if server._conversation:
        await server._conversation.shutdown()
    if server._session_mgr:
        await server._session_mgr.stop()
    if server._db:
        await server._db.close()

    logger.info("Shutdown complete")
