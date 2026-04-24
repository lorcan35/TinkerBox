"""Resource monitoring helpers + the memory-monitor periodic task.

* :func:`get_rss_mb` reads current process RSS from ``/proc/self/status``
  (no ``psutil`` dependency).
* :func:`get_cpu_temp` reads the hottest thermal zone under
  ``/sys/class/thermal`` — primary probe used to detect thermal
  throttling on Dragon Q6A (DQ03).
* :func:`memory_monitor_loop` runs forever on an :mod:`asyncio` task,
  sampling RSS + temperature + file-descriptor usage every 5 min.  On
  an RSS breach it forces GC; if still above the crit threshold it
  drains and re-inits the voice pipelines so leaked state is released
  without restarting the whole process (A04).
"""
from __future__ import annotations

import asyncio
import gc
import glob
import logging
import os
import resource
from typing import Any

logger = logging.getLogger(__name__)


def get_rss_mb() -> float:
    """Return current process RSS in MiB, or 0.0 if the read fails.

    Reads ``/proc/self/status`` so we don't pull in ``psutil`` for a
    single number.  Dragon exposes ``VmRSS`` in kilobytes; we convert.
    """
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS"):
                    return int(line.split()[1]) / 1024.0  # kB -> MB
    except Exception:
        pass
    return 0.0


def get_cpu_temp() -> float:
    """Return the hottest thermal-zone reading in °C, or 0.0 on failure.

    Tries ``thermal_zone0`` first (fastest path, common on QCS6490),
    then falls back to scanning every ``thermal_zone*/temp`` and
    returning the max.  Failures on individual zones are swallowed;
    if nothing reads, returns 0.0.
    """
    # Fast path — thermal_zone0 is usually the SoC sensor on Dragon.
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read().strip()) / 1000.0
    except Exception:
        pass

    max_temp = 0.0
    for path in glob.glob("/sys/devices/virtual/thermal/thermal_zone*/temp"):
        try:
            with open(path) as f:
                t = int(f.read().strip()) / 1000.0
                if t > max_temp:
                    max_temp = t
        except Exception:
            continue
    return max_temp


async def memory_monitor_loop(server: Any) -> None:
    """Sample RSS + temp + FD usage every 5 min; react on breaches.

    Thresholds come from ``server._mem_warn_mb`` / ``server._mem_crit_mb``.
    On RSS warn: force :func:`gc.collect` + log.
    On RSS crit after GC: drain + re-init pipelines so leaked state is
    released without restarting the whole process (A04).

    Also logs warnings if CPU ≥80°C (Gold cores throttle at ~80) or
    ≥90°C (eMMC damage risk), and if FD usage ≥80%.
    """
    from dragon_voice.pipeline import VoicePipeline

    while True:
        await asyncio.sleep(300)  # 5 minutes
        rss = get_rss_mb()
        if rss <= 0:
            continue

        active = len(server._active_connections)
        temp_c = get_cpu_temp()

        # FD count monitoring (DQ08): detect file descriptor exhaustion
        try:
            fd_count = len(os.listdir(f"/proc/{os.getpid()}/fd"))
            fd_limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
            fd_pct = (fd_count / fd_limit * 100) if fd_limit > 0 else 0
        except Exception:
            fd_count = fd_limit = 0
            fd_pct = 0.0

        logger.info(
            "Memory monitor: RSS=%.0f MB, temp=%.1f°C, connections=%d, FDs=%d/%d (%.0f%%)",
            rss, temp_c, active, fd_count, fd_limit, fd_pct,
        )

        # DQ03 thermal warnings — monitoring only, no throttling.
        if temp_c >= 90:
            logger.error(
                "Memory monitor: CPU temp %.1f°C exceeds 90°C — "
                "risk of eMMC degradation and component damage!",
                temp_c,
            )
        elif temp_c >= 80:
            logger.warning(
                "Memory monitor: CPU temp %.1f°C exceeds 80°C — "
                "Gold cores likely throttled from 2.7GHz",
                temp_c,
            )

        if fd_pct >= 80:
            logger.warning(
                "Memory monitor: FD usage at %.0f%% (%d/%d) — risk of exhaustion!",
                fd_pct, fd_count, fd_limit,
            )

        if rss > server._mem_warn_mb:
            logger.warning(
                "Memory monitor: RSS %.0f MB exceeds warning threshold (%d MB) — forcing GC",
                rss, server._mem_warn_mb,
            )
            collected = gc.collect()
            rss_after = get_rss_mb()
            logger.warning(
                "Memory monitor: GC collected %d objects, RSS now %.0f MB",
                collected, rss_after,
            )

            if rss_after > server._mem_crit_mb:
                logger.error(
                    "Memory monitor: RSS %.0f MB exceeds critical threshold (%d MB) "
                    "after GC — restarting all pipelines",
                    rss_after, server._mem_crit_mb,
                )
                for ws_id, conn in list(server._active_connections.items()):
                    pipeline = conn.get("pipeline")
                    if pipeline:
                        try:
                            await pipeline.shutdown()
                            conn["pipeline"] = None
                            logger.info("Memory monitor: shut down pipeline for %s", ws_id)
                        except Exception as e:
                            logger.warning(
                                "Memory monitor: pipeline shutdown failed for %s: %s",
                                ws_id, e,
                            )

                gc.collect()
                rss_final = get_rss_mb()
                logger.warning("Memory monitor: post-restart RSS %.0f MB", rss_final)

                # Re-initialize pipelines for registered connections.
                for ws_id, conn in list(server._active_connections.items()):
                    if conn.get("registered") and conn.get("pipeline") is None:
                        cfg = conn.get("config", server._config)
                        try:
                            pipeline = VoicePipeline(
                                cfg,
                                conn.get("_on_audio"),
                                conn.get("_on_event"),
                                conversation_engine=server._conversation,
                                session_id=conn.get("session_id", ""),
                                media_pipeline=server._media_pipeline,
                                backend_pool=server._backend_pool,
                            )
                            await pipeline.initialize()
                            conn["pipeline"] = pipeline
                            logger.info(
                                "Memory monitor: re-initialized pipeline for %s", ws_id,
                            )
                        except Exception as e:
                            logger.error(
                                "Memory monitor: pipeline re-init failed for %s: %s",
                                ws_id, e,
                            )
