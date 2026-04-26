"""System info and backend listing API routes."""

import logging
import os
import time

from aiohttp import web

from dragon_voice.config import VoiceConfig
from dragon_voice.stt import _BACKENDS as STT_BACKENDS
from dragon_voice.tts import _BACKENDS as TTS_BACKENDS
from dragon_voice.llm import _BACKENDS as LLM_BACKENDS

logger = logging.getLogger(__name__)


class SystemRoutes:
    def __init__(self, voice_config: VoiceConfig, start_time: float,
                 get_active_connections: callable, get_db=None) -> None:
        self._config = voice_config
        self._start_time = start_time
        self._get_active_connections = get_active_connections
        self._db = get_db

    def register(self, app: web.Application) -> None:
        app.router.add_get("/api/v1/system", self.system_info)
        app.router.add_get("/api/v1/backends", self.list_backends)

    async def system_info(self, request: web.Request) -> web.Response:
        """GET /api/v1/system — system metrics"""
        uptime_s = time.time() - self._start_time
        active = self._get_active_connections()

        # Memory from /proc/meminfo (no external dependency).
        # Wave 14 W14-H08: /proc reads are usually instant but get slow
        # under load (cgroup accounting on Radxa).  Offload to a thread
        # so system polls from the dashboard don't jitter the event loop.
        import asyncio as _asyncio

        def _read_meminfo():
            try:
                with open("/proc/meminfo") as f:
                    return f.read()
            except Exception:
                return ""

        def _read_loadavg():
            try:
                with open("/proc/loadavg") as f:
                    return f.read()
            except Exception:
                return ""

        meminfo_raw = await _asyncio.to_thread(_read_meminfo)
        loadavg_raw = await _asyncio.to_thread(_read_loadavg)

        mem = {"total_mb": 0, "used_mb": 0, "available_mb": 0, "percent": 0}
        if meminfo_raw:
            try:
                info = {}
                for line in meminfo_raw.splitlines():
                    parts = line.split()
                    if len(parts) >= 2:
                        info[parts[0].rstrip(":")] = int(parts[1])
                total = info.get("MemTotal", 0)
                available = info.get("MemAvailable", 0)
                mem["total_mb"] = round(total / 1024)
                mem["available_mb"] = round(available / 1024)
                mem["used_mb"] = mem["total_mb"] - mem["available_mb"]
                mem["percent"] = round((1 - available / total) * 100, 1) if total else 0
            except Exception:
                pass

        cpu_percent = 0
        if loadavg_raw:
            try:
                load_1m = float(loadavg_raw.split()[0])
                cpu_count = os.cpu_count() or 1
                cpu_percent = round(load_1m / cpu_count * 100, 1)
            except Exception:
                pass

        result = {
            "uptime_s": round(uptime_s, 1),
            "cpu_percent": cpu_percent,
            "memory": mem,
            "active_connections": active,
        }

        # Audit D3 (#137): expose inference_executor depth so the
        # dashboard can spot Moonshine / Piper backpressure (queued
        # work growing while busy is at max_workers means STT/TTS is
        # the bottleneck).  Pre-fix this was invisible — operators
        # had to guess from CPU + log timestamps.
        try:
            from dragon_voice.pipeline import inference_executor as _exec
            queue = getattr(_exec, "_work_queue", None)
            threads = getattr(_exec, "_threads", None)
            queued = queue.qsize() if queue is not None else 0
            busy = len(threads) if threads is not None else 0
            result["inference_executor"] = {
                "max_workers": getattr(_exec, "_max_workers", 0),
                "busy": busy,         # threads currently spawned
                "queued": queued,     # work items waiting for a worker
            }
        except Exception:
            # Don't let metrics-collection break the endpoint.
            pass

        # DB stats if available
        if self._db:
            try:
                cursor = await self._db.conn.execute("SELECT COUNT(*) FROM sessions")
                row = await cursor.fetchone()
                result["total_sessions"] = row[0] if row else 0

                cursor = await self._db.conn.execute("SELECT COUNT(*) FROM messages")
                row = await cursor.fetchone()
                result["total_messages"] = row[0] if row else 0
            except Exception:
                pass

        return web.json_response(result)

    async def list_backends(self, request: web.Request) -> web.Response:
        """GET /api/v1/backends — available STT/TTS/LLM backends"""
        return web.json_response({
            "stt": {
                "active": self._config.stt.backend,
                "available": sorted(STT_BACKENDS.keys()),
            },
            "tts": {
                "active": self._config.tts.backend,
                "available": sorted(TTS_BACKENDS.keys()),
            },
            "llm": {
                "active": self._config.llm.backend,
                "available": sorted(LLM_BACKENDS.keys()),
            },
        })
