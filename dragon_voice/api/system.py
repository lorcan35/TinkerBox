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

        # Memory from /proc/meminfo (no external dependency)
        mem = {"total_mb": 0, "used_mb": 0, "available_mb": 0, "percent": 0}
        try:
            with open("/proc/meminfo") as f:
                info = {}
                for line in f:
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

        # CPU from /proc/stat (simple instant snapshot)
        cpu_percent = 0
        try:
            with open("/proc/loadavg") as f:
                load_1m = float(f.read().split()[0])
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
