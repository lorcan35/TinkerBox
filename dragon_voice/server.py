"""WebSocket server for Dragon Voice.

Serves the voice pipeline over WebSocket and provides HTTP endpoints
for health checks, status, configuration, and the REST API.

Integrates: Database, SessionManager, MessageStore, ConversationEngine, API routes.

refs #16, #17, #18
"""

import asyncio
import copy
import gc
import json
import logging
import os
import resource
import time
from typing import Optional

import aiohttp
from aiohttp import web, WSMsgType

from dragon_voice.api import setup_all_routes
from dragon_voice.media.store import MediaStore
from dragon_voice.media.pipeline import MediaPipeline
from dragon_voice.config import (
    VoiceConfig, config_to_dict, load_config,
    SYSTEM_PROMPT_LOCAL, SYSTEM_PROMPT_HYBRID, SYSTEM_PROMPT_CLOUD,
    MAX_TOKENS_LOCAL, MAX_TOKENS_HYBRID, MAX_TOKENS_CLOUD,
)
from dragon_voice.conversation import ConversationEngine
from dragon_voice.db import Database
from dragon_voice.messages import MessageStore
from dragon_voice.pipeline import VoicePipeline
from dragon_voice.sessions import SessionManager

logger = logging.getLogger(__name__)


class VoiceServer:
    """Aiohttp-based WebSocket server for the Dragon Voice pipeline.

    Manages device registration, session lifecycle, conversation persistence,
    and the voice pipeline (STT -> LLM -> TTS).
    """

    def __init__(self, config: VoiceConfig) -> None:
        self._config = config
        self._app: Optional[web.Application] = None
        self._start_time = time.time()

        # Legacy counters (kept for backward compat on status page)
        self._session_count = 0

        # Active WebSocket sessions: ws_id -> {pipeline, session_id, device_id}
        self._active_connections: dict[str, dict] = {}
        self._max_connections = 10
        self._purge_task: Optional[asyncio.Task] = None
        self._memory_monitor_task: Optional[asyncio.Task] = None

        # Memory thresholds (MB) — Dragon has 8GB total, Ollama ~1.5GB, TinkerClaw ~300MB
        self._mem_warn_mb = 2048   # Force GC above this
        self._mem_crit_mb = 3072   # Restart pipeline above this (after GC)

        # Backend names for status page
        self._stt_name = config.stt.backend
        self._tts_name = config.tts.backend
        self._llm_name = config.llm.backend

        # Shared aiohttp client session for dashboard proxy (DQ08: avoids per-request FD churn)
        self._proxy_session: Optional[aiohttp.ClientSession] = None

        # Foundation modules (initialized in on_startup)
        self._db: Optional[Database] = None
        self._session_mgr: Optional[SessionManager] = None
        self._message_store: Optional[MessageStore] = None
        self._conversation: Optional[ConversationEngine] = None
        self._notes_svc = None

        # Media handling (rich media detection + user image uploads)
        self._media_store = MediaStore()
        self._media_pipeline = MediaPipeline(self._media_store)
        self._media_cleanup_task: Optional[asyncio.Task] = None

    def create_app(self) -> web.Application:
        """Create and configure the aiohttp application."""
        app = web.Application(
            client_max_size=32 * 1024 * 1024,  # 32MB for audio uploads
            middlewares=[self._cors_middleware],
        )

        # HTTP routes (legacy)
        app.router.add_get("/", self._handle_status)
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/api/config", self._handle_get_config)
        app.router.add_post("/api/config", self._handle_set_config)

        # Dashboard proxy — forwards /dashboard* to localhost:3500
        app.router.add_route("*", "/dashboard{path:.*}", self._proxy_dashboard)

        # WebSocket route
        app.router.add_get("/ws/voice", self._handle_ws_voice)

        # Lifecycle hooks
        app.on_startup.append(self._on_startup)
        app.on_shutdown.append(self._on_shutdown)

        self._app = app
        return app

    # Allowed CORS origins — only these can make cross-origin API calls
    _CORS_ALLOWED_ORIGINS = {
        "http://localhost:3500",
        "http://127.0.0.1:3500",
        "http://192.168.1.90:8080",
        "https://tinkerclaw-dashboard.ngrok.dev",
    }

    @web.middleware
    async def _cors_middleware(self, request: web.Request, handler):
        """Add CORS headers to API responses for allowed origins only (SEC12)."""
        origin = request.headers.get("Origin", "")

        # If origin is not in the allowlist, skip CORS headers entirely
        # (browser will block the cross-origin request)
        if origin not in self._CORS_ALLOWED_ORIGINS:
            if request.method == "OPTIONS":
                return web.Response(status=403)
            return await handler(request)

        # Handle preflight OPTIONS requests for allowed origins
        if request.method == "OPTIONS":
            return web.Response(headers={
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, X-Sample-Rate, Accept",
                "Access-Control-Max-Age": "3600",
            })
        response = await handler(request)
        response.headers["Access-Control-Allow-Origin"] = origin
        return response

    # --------------------------------------------------------------- Dashboard proxy

    async def _proxy_dashboard(self, request: web.Request) -> web.StreamResponse:
        """Reverse proxy /dashboard* to the dashboard on localhost:3500.

        Rewrites paths: /dashboard/foo → /foo on port 3500.
        This lets the dashboard be accessed via the ngrok tunnel at
        https://tinkerbox.ngrok.dev/dashboard without a separate tunnel.
        """
        path = request.match_info.get("path", "")
        target = f"http://127.0.0.1:3500{path}"
        if request.query_string:
            target += f"?{request.query_string}"

        try:
            session = self._proxy_session
            if session is None or session.closed:
                session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
                self._proxy_session = session

            method = request.method
            headers = {k: v for k, v in request.headers.items()
                       if k.lower() not in ("host", "content-length", "transfer-encoding")}
            body = await request.read() if request.can_read_body else None

            async with session.request(method, target, headers=headers, data=body) as resp:
                response = web.StreamResponse(
                    status=resp.status,
                    headers={k: v for k, v in resp.headers.items()
                             if k.lower() not in ("transfer-encoding", "content-encoding")},
                )
                response.content_type = resp.content_type
                await response.prepare(request)
                async for chunk in resp.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
                return response
        except Exception as e:
            logger.warning("Dashboard proxy failed: %s", e)
            return web.json_response(
                {"error": f"Dashboard not reachable: {e}"},
                status=502,
            )

    # --------------------------------------------------------------- Lifecycle

    async def _on_startup(self, app: web.Application) -> None:
        """Initialize foundation modules on server start."""
        logger.info("Initializing foundation modules...")

        # Shared client session for dashboard proxy (DQ08)
        self._proxy_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        )

        # Database
        self._db = Database()
        await self._db.initialize()

        # Session manager (with background cleanup)
        self._session_mgr = SessionManager(self._db)
        await self._session_mgr.start()

        # Message store
        self._message_store = MessageStore(self._db)

        # Memory service (agentic: facts + documents + RAG)
        self._memory_service = None
        self._tool_registry = None
        try:
            from dragon_voice.memory import MemoryService
            from dragon_voice.tools import ToolRegistry
            from dragon_voice.tools.web_search import WebSearchTool
            from dragon_voice.tools.datetime_tool import DateTimeTool

            self._memory_service = MemoryService(
                self._db,
                ollama_url=self._config.llm.ollama_url,
            )
            await self._memory_service.initialize()

            self._tool_registry = ToolRegistry()
            self._tool_registry.register(WebSearchTool(
                searxng_url=getattr(self._config.tools, "searxng_url", "")
            ))
            self._tool_registry.register(DateTimeTool())

            # Memory tools need memory_service
            from dragon_voice.tools.memory_tools import StoreFactTool, RecallFactsTool
            self._tool_registry.register(StoreFactTool(self._memory_service))
            self._tool_registry.register(RecallFactsTool(self._memory_service))

            # Tier 1 tools
            from dragon_voice.tools.timer_tool import TimerTool
            from dragon_voice.tools.weather_tool import WeatherTool
            from dragon_voice.tools.calculator_tool import CalculatorTool
            from dragon_voice.tools.unit_converter_tool import UnitConverterTool
            from dragon_voice.tools.note_tool import NoteTool
            from dragon_voice.tools.system_tool import SystemInfoTool

            self._tool_registry.register(TimerTool())
            self._tool_registry.register(WeatherTool())
            self._tool_registry.register(CalculatorTool())
            self._tool_registry.register(UnitConverterTool())
            self._tool_registry.register(SystemInfoTool())

            logger.info("Agentic modules initialized (tools: %d, memory: ok)",
                        len(self._tool_registry.list_tools()))
        except Exception as e:
            logger.warning("Agentic modules not available: %s", e)

        # Conversation engine (shared LLM backend for text/API input)
        self._conversation = ConversationEngine(
            self._db, self._message_store, self._config.llm,
            tool_registry=self._tool_registry,
            memory_service=self._memory_service,
        )
        await self._conversation.initialize()

        # REST API routes (modular package)
        setup_all_routes(
            app,
            db=self._db,
            session_mgr=self._session_mgr,
            message_store=self._message_store,
            conversation=self._conversation,
            voice_config=self._config,
            start_time=self._start_time,
            get_active_connections=lambda: len(self._active_connections),
            tool_registry=self._tool_registry,
            memory_service=self._memory_service,
            media_store=self._media_store,
        )

        # Notes API routes
        try:
            from dragon_voice.notes.db import NotesDB
            from dragon_voice.notes.service import NotesService
            from dragon_voice.notes.api import setup_routes as setup_notes_routes

            notes_db = NotesDB()
            notes_db.initialize()
            notes_svc = NotesService(self._config, notes_db)
            await notes_svc.initialize()
            self._notes_svc = notes_svc  # Store for shutdown
            setup_notes_routes(app, notes_svc)
            logger.info("Notes API routes registered")

            # Register note tool now that NotesService is available
            if self._tool_registry and self._notes_svc:
                from dragon_voice.tools.note_tool import NoteTool
                self._tool_registry.register(NoteTool(self._notes_svc))
                logger.info("Note tool registered (notes service available)")
        except Exception as e:
            logger.warning("Notes API not available: %s", e)

        # MCP servers (from config)
        try:
            from dragon_voice.mcp.bridge import bridge_mcp_server
            mcp_servers = getattr(self._config, 'mcp_servers', [])
            for mcp in mcp_servers:
                count = await bridge_mcp_server(
                    self._tool_registry,
                    name=mcp.get('name', 'mcp'),
                    url=mcp.get('url'),
                    token=mcp.get('token'),
                )
                logger.info("MCP %s: %d tools bridged", mcp.get('name'), count)
        except Exception as e:
            logger.warning("MCP bridge not available: %s", e)

        # Run initial message purge and schedule periodic purge (US-DQ14)
        retention_days = self._config.database.message_retention_days
        if retention_days > 0:
            try:
                result = await self._db.purge_old_messages(days=retention_days)
                logger.info(
                    "Startup purge complete: %d messages, %d events removed (retention=%d days)",
                    result["messages"], result["events"], retention_days,
                )
            except Exception as e:
                logger.warning("Startup purge failed: %s", e)

            self._purge_task = asyncio.create_task(
                self._periodic_purge(retention_days)
            )

        # Media cleanup (hourly, removes expired uploads)
        self._media_cleanup_task = asyncio.create_task(self._media_cleanup_loop())

        # Start periodic memory monitor (A04)
        self._memory_monitor_task = asyncio.create_task(self._memory_monitor())
        rss = self._get_rss_mb()
        logger.info("Memory monitor started (RSS=%.0f MB, warn=%d MB, crit=%d MB)",
                     rss, self._mem_warn_mb, self._mem_crit_mb)

        logger.info("Foundation modules initialized")

    async def _periodic_purge(self, days: int) -> None:
        """Run message/event purge every 24 hours."""
        while True:
            await asyncio.sleep(86400)  # 24 hours
            if self._db is None:
                break
            try:
                result = await self._db.purge_old_messages(days=days)
                logger.info(
                    "Periodic purge: %d messages, %d events removed",
                    result["messages"], result["events"],
                )
            except Exception as e:
                logger.warning("Periodic purge failed: %s", e)

    async def _media_cleanup_loop(self):
        """Remove expired media uploads every hour."""
        while True:
            await asyncio.sleep(3600)
            try:
                await self._media_store.cleanup()
            except Exception as e:
                logger.warning("Media cleanup error: %s", e)

    @staticmethod
    def _get_rss_mb() -> float:
        """Read current process RSS from /proc/self/status (no psutil dependency)."""
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS"):
                        return int(line.split()[1]) / 1024.0  # kB -> MB
        except Exception:
            pass
        return 0.0

    @staticmethod
    def _get_cpu_temp() -> float:
        """Read CPU temperature from thermal zone sysfs (DQ03).

        Tries thermal_zone0 first (common on QCS6490), then scans all
        thermal zones for the highest reading.  Returns 0.0 on failure.
        """
        # Try thermal_zone0 first (fastest path)
        try:
            with open('/sys/class/thermal/thermal_zone0/temp') as f:
                temp_mc = int(f.read().strip())
                return temp_mc / 1000.0
        except Exception:
            pass

        # Fallback: scan all thermal zones, return the highest
        import glob
        max_temp = 0.0
        for path in glob.glob('/sys/devices/virtual/thermal/thermal_zone*/temp'):
            try:
                with open(path) as f:
                    temp_mc = int(f.read().strip())
                    t = temp_mc / 1000.0
                    if t > max_temp:
                        max_temp = t
            except Exception:
                continue
        return max_temp

    async def _memory_monitor(self) -> None:
        """Periodic memory check every 5 minutes (A04).

        - Log RSS and CPU temperature at INFO level
        - If RSS > warn threshold: force gc.collect() and log WARNING
        - If RSS > critical threshold after GC: gracefully restart all pipelines
        - DQ03: Log CPU temperature warnings at 80°C and errors at 90°C
        """
        while True:
            await asyncio.sleep(300)  # 5 minutes
            rss = self._get_rss_mb()
            if rss <= 0:
                continue

            active = len(self._active_connections)

            # CPU temperature monitoring (DQ03): detect thermal throttling
            temp_c = self._get_cpu_temp()

            # FD count monitoring (DQ08): detect file descriptor exhaustion
            try:
                fd_count = len(os.listdir(f'/proc/{os.getpid()}/fd'))
                fd_limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
                fd_pct = (fd_count / fd_limit * 100) if fd_limit > 0 else 0
            except Exception:
                fd_count = fd_limit = 0
                fd_pct = 0.0

            logger.info(
                "Memory monitor: RSS=%.0f MB, temp=%.1f°C, connections=%d, FDs=%d/%d (%.0f%%)",
                rss, temp_c, active, fd_count, fd_limit, fd_pct,
            )

            # DQ03: Thermal warnings — monitoring only, no throttling
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

            if rss > self._mem_warn_mb:
                logger.warning(
                    "Memory monitor: RSS %.0f MB exceeds warning threshold (%d MB) — forcing GC",
                    rss, self._mem_warn_mb,
                )
                collected = gc.collect()
                rss_after = self._get_rss_mb()
                logger.warning(
                    "Memory monitor: GC collected %d objects, RSS now %.0f MB",
                    collected, rss_after,
                )

                if rss_after > self._mem_crit_mb:
                    logger.error(
                        "Memory monitor: RSS %.0f MB exceeds critical threshold (%d MB) "
                        "after GC — restarting all pipelines",
                        rss_after, self._mem_crit_mb,
                    )
                    # Gracefully restart every active pipeline
                    for ws_id, conn in list(self._active_connections.items()):
                        pipeline = conn.get("pipeline")
                        if pipeline:
                            try:
                                await pipeline.shutdown()
                                conn["pipeline"] = None
                                logger.info("Memory monitor: shut down pipeline for %s", ws_id)
                            except Exception as e:
                                logger.warning("Memory monitor: pipeline shutdown failed for %s: %s", ws_id, e)

                    # Force another GC after pipeline shutdown
                    gc.collect()
                    rss_final = self._get_rss_mb()
                    logger.warning("Memory monitor: post-restart RSS %.0f MB", rss_final)

                    # Re-initialize pipelines for registered connections
                    for ws_id, conn in list(self._active_connections.items()):
                        if conn.get("registered") and conn.get("pipeline") is None:
                            cfg = conn.get("config", self._config)
                            try:
                                pipeline = VoicePipeline(
                                    cfg,
                                    conn.get("_on_audio"),
                                    conn.get("_on_event"),
                                    conversation_engine=self._conversation,
                                    session_id=conn.get("session_id", ""),
                                    media_pipeline=self._media_pipeline,
                                )
                                await pipeline.initialize()
                                conn["pipeline"] = pipeline
                                logger.info("Memory monitor: re-initialized pipeline for %s", ws_id)
                            except Exception as e:
                                logger.error("Memory monitor: pipeline re-init failed for %s: %s", ws_id, e)

    async def _on_shutdown(self, app: web.Application) -> None:
        """Clean up all active sessions and foundation modules on server shutdown."""
        logger.info("Server shutting down — closing %d connections", len(self._active_connections))

        # Cancel periodic tasks
        if self._memory_monitor_task and not self._memory_monitor_task.done():
            self._memory_monitor_task.cancel()
        if self._purge_task and not self._purge_task.done():
            self._purge_task.cancel()

        # Shut down pipelines
        tasks = []
        for ws_id, conn in list(self._active_connections.items()):
            pipeline = conn.get("pipeline")
            if pipeline:
                tasks.append(pipeline.shutdown())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active_connections.clear()

        # Close shared proxy session (DQ08)
        if self._proxy_session and not self._proxy_session.closed:
            await self._proxy_session.close()

        # Shut down foundation
        if self._notes_svc:
            await self._notes_svc.shutdown()
        if self._memory_service:
            logger.info("Shutting down memory service")
            # MemoryService doesn't have explicit shutdown but clear reference
            self._memory_service = None
        if self._conversation:
            await self._conversation.shutdown()
        if self._session_mgr:
            await self._session_mgr.stop()
        if self._db:
            await self._db.close()

        logger.info("Shutdown complete")

    # ------------------------------------------------------------------ HTTP

    async def _handle_status(self, request: web.Request) -> web.Response:
        """Status page with backend info and uptime."""
        uptime = time.time() - self._start_time
        hours = int(uptime // 3600)
        minutes = int((uptime % 3600) // 60)
        seconds = int(uptime % 60)

        html = f"""<!DOCTYPE html>
<html>
<head><title>Dragon Voice Server</title>
<style>
  body {{ font-family: monospace; background: #1a1a2e; color: #e0e0e0; padding: 2em; }}
  h1 {{ color: #ff6b35; }}
  .info {{ background: #16213e; padding: 1em; border-radius: 8px; margin: 1em 0; }}
  .label {{ color: #0f3460; font-weight: bold; }}
  span.val {{ color: #53d769; }}
</style>
</head>
<body>
  <h1>Dragon Voice Server</h1>
  <div class="info">
    <p>STT Backend: <span class="val">{self._stt_name}</span></p>
    <p>TTS Backend: <span class="val">{self._tts_name}</span></p>
    <p>LLM Backend: <span class="val">{self._llm_name}</span></p>
    <p>Uptime: <span class="val">{hours}h {minutes}m {seconds}s</span></p>
    <p>Active Connections: <span class="val">{len(self._active_connections)}</span></p>
    <p>Total Sessions: <span class="val">{self._session_count}</span></p>
  </div>
</body>
</html>"""
        return web.Response(text=html, content_type="text/html")

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint returning JSON."""
        return web.json_response(
            {
                "status": "ok",
                "uptime_seconds": round(time.time() - self._start_time, 1),
                "active_connections": len(self._active_connections),
                "backends": {
                    "stt": self._stt_name,
                    "tts": self._tts_name,
                    "llm": self._llm_name,
                },
            }
        )

    async def _handle_get_config(self, request: web.Request) -> web.Response:
        """Return current config with secrets redacted."""
        return web.json_response(
            config_to_dict(self._config, redact_secrets=True)
        )

    async def _handle_set_config(self, request: web.Request) -> web.Response:
        """Hot-reload configuration.

        Accepts a partial config JSON — only provided sections are updated.
        Swaps backends on active sessions if needed.
        """
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response(
                {"error": "Invalid JSON"}, status=400
            )

        logger.info("Config update requested: %s", list(body.keys()))

        try:
            # Reload full config from file first, then apply overrides
            new_config = load_config()

            # Apply overrides from the request body
            if "stt" in body:
                for k, v in body["stt"].items():
                    if hasattr(new_config.stt, k):
                        setattr(new_config.stt, k, v)
            if "tts" in body:
                for k, v in body["tts"].items():
                    if hasattr(new_config.tts, k):
                        setattr(new_config.tts, k, v)
            if "llm" in body:
                for k, v in body["llm"].items():
                    if hasattr(new_config.llm, k):
                        setattr(new_config.llm, k, v)
            if "audio" in body:
                for k, v in body["audio"].items():
                    if hasattr(new_config.audio, k):
                        setattr(new_config.audio, k, v)

            # Validate before applying
            validation_errors = new_config.validate()
            if validation_errors:
                return web.json_response(
                    {"error": "Config validation failed", "details": validation_errors},
                    status=400,
                )

            old_config = self._config
            self._config = new_config

            # Update displayed backend names
            self._stt_name = new_config.stt.backend
            self._tts_name = new_config.tts.backend
            self._llm_name = new_config.llm.backend

            # Swap backends on all active pipelines.
            # A06: Acquire each connection's conn_lock before swapping to
            # prevent races with WS config_update on the same connection.
            # Two concurrent swap_backends() calls would interleave
            # shutdown/init of backends, causing use-after-free errors.
            swap_errors = []
            for ws_id, conn in list(self._active_connections.items()):
                pipeline = conn.get("pipeline")
                if pipeline:
                    lock = conn.get("conn_lock")
                    logger.info("Swapping backends for connection %s", ws_id)
                    try:
                        if lock:
                            async with lock:
                                await pipeline.swap_backends(new_config)
                        else:
                            await pipeline.swap_backends(new_config)
                    except Exception as e:
                        logger.warning("Backend swap failed for %s: %s", ws_id, e)
                        swap_errors.append(str(e))

            pipelines_with_swap = sum(
                1 for c in self._active_connections.values() if c.get("pipeline")
            )
            return web.json_response(
                {
                    "status": "ok",
                    "message": f"Config updated, {pipelines_with_swap} pipelines reloaded",
                    "backends": {
                        "stt": new_config.stt.backend,
                        "tts": new_config.tts.backend,
                        "llm": new_config.llm.backend,
                    },
                }
            )

        except Exception as e:
            logger.exception("Config update failed")
            return web.json_response(
                {"error": str(e)}, status=500
            )

    # --------------------------------------------------------------- WebSocket

    async def _handle_ws_voice(self, request: web.Request) -> web.WebSocketResponse:
        """Main voice WebSocket endpoint.

        Protocol (see docs/protocol.md):
          Tab5 -> Dragon:
            - JSON: register, start, stop, cancel, text, record_start, record_stop
            - Binary: raw PCM int16 16kHz mono audio

          Dragon -> Tab5:
            - JSON: session_start, stt, llm, tts_start, tts_end, note_created,
                    config_update, error, event
            - Binary: PCM int16 audio at config.tts_sample_rate
        """
        # Reject if at connection limit
        if len(self._active_connections) >= self._max_connections:
            logger.warning("Connection limit reached (%d), rejecting", self._max_connections)
            return web.Response(text="Too many connections", status=503)

        ws = web.WebSocketResponse(
            max_msg_size=10 * 1024 * 1024,  # 10MB max message
            heartbeat=None,  # DISABLED: although ESP-IDF v5.4.3 auto-PONGs, the latency through
            # ngrok (200-500ms) plus SSL overhead causes spurious timeouts. Keepalive handled
            # by _ws_keepalive task (20s ws.ping) + Tab5 JSON pings (8s).
        )
        await ws.prepare(request)

        # Per-connection config: deep copy so mutations (voice_mode switch,
        # model changes) don't bleed between connections.  self._config
        # remains the immutable server default for new connections.
        conn_config = copy.deepcopy(self._config)

        # Per-connection processing lock (US-P10): serializes voice and text
        # requests so they don't corrupt shared state (conversation context,
        # LLM backend, TTS output path).  One lock per connection — different
        # devices are independent.
        conn_lock = asyncio.Lock()

        ws_id = f"ws{self._session_count}"
        self._session_count += 1
        peer = request.remote or "unknown"
        logger.info("WebSocket connected: %s (ws_id=%s)", peer, ws_id)

        # Server-side keepalive: ping every 15s to prevent ngrok idle timeout.
        # ngrok drops WS connections after ~30s of silence. Detects dead
        # connections via send-failure counter + response timeout (US-DQ20).
        _keepalive_running = True
        # Shared flag: last time ANY message was received from the client.
        # Updated in the main message loop; read by the keepalive task.
        _last_client_msg_time = time.monotonic()

        async def _ws_keepalive():
            nonlocal _last_client_msg_time
            fail_count = 0
            while _keepalive_running and not ws.closed:
                await asyncio.sleep(15)  # 15s < ngrok's ~30s idle threshold
                if ws.closed or not _keepalive_running:
                    break
                # Send JSON pong (data frame) — ngrok counts data frames as activity.
                # 5s timeout prevents sends from blocking indefinitely when the
                # event loop is delayed by GIL contention from inference (US-DQ05).
                try:
                    await asyncio.wait_for(
                        ws.send_json({"type": "pong"}), timeout=5.0
                    )
                    fail_count = 0
                except asyncio.TimeoutError:
                    fail_count += 1
                    logger.warning(
                        "Keepalive send timed out for %s (%d/3) — event loop may be blocked",
                        ws_id, fail_count,
                    )
                    if fail_count >= 3:
                        logger.warning("Keepalive: 3 consecutive timeouts, closing WS %s", ws_id)
                        try:
                            await ws.close()
                        except Exception:
                            pass
                        break
                    continue
                except Exception:
                    fail_count += 1
                    logger.warning("Keepalive send failed for %s (%d/3)", ws_id, fail_count)
                    if fail_count >= 3:
                        logger.warning("Keepalive: 3 consecutive send failures, closing WS %s", ws_id)
                        try:
                            await ws.close()
                        except Exception:
                            pass
                        break
                    continue

                # Response timeout: Tab5 sends pings every 8s.  If we haven't
                # received ANY message in 30s the connection is dead.
                silence = time.monotonic() - _last_client_msg_time
                if silence > 30:
                    logger.warning("Keepalive: no client message for %.0fs, closing WS %s", silence, ws_id)
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    break

        _keepalive_task = asyncio.create_task(_ws_keepalive())

        # Connection state — populated after register
        conn_state: dict = {
            "ws_id": ws_id,
            "pipeline": None,
            "session_id": None,
            "device_id": None,
            "registered": False,
            "mode": "ask",  # "ask" or "dictate"
            "config": conn_config,  # per-connection config (deep copy of server default)
            "conn_lock": conn_lock,  # A06: stored so HTTP config handler can serialize
            "_on_audio": None,   # stored for pipeline re-init (A04)
            "_on_event": None,
        }
        self._active_connections[ws_id] = conn_state

        # Callbacks for the pipeline
        async def on_audio(audio_bytes: bytes) -> None:
            if not ws.closed:
                try:
                    await ws.send_bytes(audio_bytes)
                except Exception:
                    logger.warning("Failed to send audio to %s", ws_id)

        async def on_event(event: dict) -> None:
            if not ws.closed:
                try:
                    await ws.send_json(event)
                except Exception:
                    logger.warning("Failed to send event to %s", ws_id)
            # Persist API usage events for cost tracking
            if event.get("type") == "api_usage" and self._db:
                try:
                    await self._db.add_event(
                        "api_usage",
                        session_id=conn_state.get("session_id"),
                        device_id=conn_state.get("device_id"),
                        data={k: v for k, v in event.items() if k != "type"},
                    )
                except Exception as e:
                    logger.debug("Callback error: %s", e)

        # Store callback refs for pipeline re-init (A04 memory monitor)
        conn_state["_on_audio"] = on_audio
        conn_state["_on_event"] = on_event

        try:
            async for msg in ws:
                _last_client_msg_time = time.monotonic()

                if msg.type == WSMsgType.BINARY:
                    # Raw PCM audio data — forward to pipeline
                    # Note: feed_audio is NOT locked (US-P10) — it only
                    # appends to the audio buffer and the VAD check is
                    # lightweight. The heavy processing (_process_utterance)
                    # is triggered via asyncio.create_task inside feed_audio
                    # and that task is serialized by the pipeline's own
                    # _processing flag. Locking here would block audio
                    # ingestion during LLM/TTS processing.
                    pipeline = conn_state.get("pipeline")
                    if pipeline:
                        await pipeline.feed_audio(msg.data)

                elif msg.type == WSMsgType.TEXT:
                    try:
                        cmd = json.loads(msg.data)
                    except json.JSONDecodeError:
                        logger.warning("Invalid JSON from %s: %s", ws_id, msg.data[:100])
                        continue

                    cmd_type = cmd.get("type", "")

                    if cmd_type == "register":
                        await self._handle_register(ws, conn_state, cmd, on_audio, on_event, conn_config)

                    elif cmd_type == "start":
                        pipeline = conn_state.get("pipeline")
                        if pipeline:
                            mode = cmd.get("mode", "ask")
                            conn_state["mode"] = mode
                            pipeline._audio_buffer.clear()
                            pipeline._dictation_mode = (mode == "dictate")
                            if mode == "dictate":
                                pipeline._segment_buffer.clear()
                                pipeline._dictation_segments.clear()
                            logger.info("Connection %s: start (mode=%s, audio buffer cleared)", ws_id, mode)

                    elif cmd_type == "segment":
                        pipeline = conn_state.get("pipeline")
                        if pipeline and conn_state.get("mode") == "dictate":
                            logger.info("Connection %s: segment marker", ws_id)
                            async with conn_lock:  # US-P10: serialize with text
                                await pipeline.process_segment()

                    elif cmd_type == "stop":
                        pipeline = conn_state.get("pipeline")
                        if pipeline:
                            mode = conn_state.get("mode", "ask")
                            buf_size = len(pipeline._audio_buffer) + len(pipeline._segment_buffer)
                            logger.info("Connection %s: stop (mode=%s, buffer=%d bytes)", ws_id, mode, buf_size)
                            async with conn_lock:  # US-P10: serialize with text
                                if mode == "dictate":
                                    transcript = await pipeline.finish_dictation()
                                    # Auto-save dictation to Dragon notes DB
                                    if transcript and len(transcript.strip()) > 10 and self._notes_svc:
                                        try:
                                            note = await self._notes_svc.create_from_text(
                                                transcript.strip(), title=""
                                            )
                                            logger.info("Auto-created note %s from dictation (%d chars)",
                                                        note.id, len(transcript))
                                            if not ws.closed:
                                                await ws.send_json({
                                                    "type": "note_created",
                                                    "note_id": note.id,
                                                    "title": note.title,
                                                    "transcript": transcript[:200],
                                                })
                                        except Exception as e:
                                            logger.error("Failed to auto-create dictation note: %s", e)
                                else:
                                    await pipeline.start_processing()

                    elif cmd_type == "clear":
                        pipeline = conn_state.get("pipeline")
                        if pipeline:
                            pipeline.clear_history()
                        # End current session and create a fresh one (clears DB context)
                        old_sid = conn_state.get("session_id")
                        device_id = conn_state.get("device_id")
                        if old_sid and self._session_mgr:
                            await self._session_mgr.end_session(old_sid)
                            session, _ = await self._session_mgr.create_session(
                                device_id=device_id, session_type="conversation"
                            )
                            conn_state["session_id"] = session["id"]
                            logger.info("Connection %s: history cleared, new session %s",
                                        ws_id, session["id"])
                            if not ws.closed:
                                await ws.send_json({
                                    "type": "session_start",
                                    "session_id": session["id"],
                                    "device_id": device_id,
                                    "resumed": False,
                                    "message_count": 0,
                                })
                        else:
                            logger.info("Connection %s: conversation history cleared", ws_id)

                    elif cmd_type == "cancel":
                        pipeline = conn_state.get("pipeline")
                        if pipeline:
                            logger.info("Connection %s: cancel", ws_id)
                            await pipeline.cancel()

                    elif cmd_type == "text":
                        async with conn_lock:  # US-P10: serialize with voice
                            await self._handle_text(ws, conn_state, cmd)

                    elif cmd_type == "user_media":
                        async with conn_lock:
                            await self._handle_user_media(ws, conn_state, cmd)

                    elif cmd_type == "record_start" or cmd_type == "record_stop":
                        # Superseded by dictation mode (start with mode=dictate)
                        logger.info("Connection %s: %s (use mode=dictate instead)", ws_id, cmd_type)

                    elif cmd_type == "ping":
                        # ESP-IDF sends application-level pings (LEARNINGS.md #11)
                        await ws.send_json({"type": "pong"})

                    elif cmd_type == "config_update":
                        # US-P01: Acquire conn_lock to serialize with stop/text
                        # handlers. Prevents swap_backends() from running while
                        # start_processing() or finish_dictation() is in flight.
                        # Three-tier voice mode: 0=local, 1=hybrid, 2=cloud
                        voice_mode = cmd.get("voice_mode")
                        llm_model = cmd.get("llm_model")
                        # Backward compat: old binary cloud_mode toggle
                        cloud_mode = cmd.get("cloud_mode")
                        if cloud_mode is not None and voice_mode is None:
                            voice_mode = 2 if cloud_mode else 0

                        if voice_mode is not None:
                            # STT+TTS: local for mode 0, cloud for mode 1+2+3
                            if voice_mode == 0:
                                stt_be, tts_be = "moonshine", "piper"
                            elif voice_mode == 3:
                                # TinkerClaw mode: default local STT/TTS
                                # "cloud" suffix in llm_model → use OpenRouter STT/TTS
                                if llm_model and "cloud" in llm_model.lower():
                                    stt_be, tts_be = "openrouter", "openrouter"
                                else:
                                    stt_be, tts_be = "moonshine", "piper"
                            else:
                                stt_be, tts_be = "openrouter", "openrouter"

                            # LLM backend selection
                            if voice_mode == 3:
                                # TinkerClaw mode — gateway handles everything
                                llm_be = "tinkerclaw"
                                if llm_model:
                                    conn_config.llm.tinkerclaw_model = llm_model
                            elif voice_mode == 2:
                                llm_be = "openrouter"
                                if llm_model:
                                    conn_config.llm.openrouter_model = llm_model
                            else:
                                llm_be = conn_config.llm.local_backend or "ollama"
                                if llm_model and llm_be == "ollama" and "/" not in llm_model:
                                    conn_config.llm.ollama_model = llm_model
                                    logger.info("Local model switched to: %s", llm_model)

                            # Apply mode-aware system prompt and max_tokens
                            # Mode 3 (TinkerClaw): skip — TinkerClaw owns personality
                            if voice_mode == 3:
                                pass  # TinkerClaw manages its own prompts and limits
                            elif voice_mode == 0:
                                conn_config.llm.system_prompt = SYSTEM_PROMPT_LOCAL
                                conn_config.llm.max_tokens = MAX_TOKENS_LOCAL
                            elif voice_mode == 1:
                                conn_config.llm.system_prompt = SYSTEM_PROMPT_HYBRID
                                conn_config.llm.max_tokens = MAX_TOKENS_HYBRID
                            else:
                                conn_config.llm.system_prompt = SYSTEM_PROMPT_CLOUD
                                conn_config.llm.max_tokens = MAX_TOKENS_CLOUD

                            logger.info("Connection %s: voice_mode=%d → stt=%s tts=%s llm=%s model=%s tokens=%d",
                                        ws_id, voice_mode, stt_be, tts_be, llm_be,
                                        conn_config.llm.openrouter_model if voice_mode == 2 else "(local)",
                                        conn_config.llm.max_tokens)

                            # Validate TinkerClaw gateway is reachable before switching to mode 3
                            if voice_mode == 3:
                                try:
                                    tc_url = (conn_config.llm.tinkerclaw_url or "http://localhost:18789").rstrip("/")
                                    async with aiohttp.ClientSession(
                                        timeout=aiohttp.ClientTimeout(total=5)
                                    ) as tc_session:
                                        async with tc_session.get(f"{tc_url}/health") as tc_resp:
                                            if tc_resp.status != 200:
                                                logger.error("TinkerClaw health check returned %d", tc_resp.status)
                                                raise RuntimeError(f"health check returned {tc_resp.status}")
                                    logger.info("TinkerClaw gateway health OK at %s", tc_url)
                                except Exception as tc_err:
                                    logger.error("TinkerClaw gateway not reachable: %s", tc_err)
                                    if not ws.closed:
                                        await ws.send_json({
                                            "type": "config_update",
                                            "error": "TinkerClaw gateway is not reachable",
                                            "voice_mode": voice_mode,
                                        })
                                    continue

                            # Validate API key for cloud modes (1=Hybrid, 2=Cloud need OpenRouter)
                            # Mode 3 (TinkerClaw) doesn't need Dragon's OpenRouter key — uses own gateway
                            if voice_mode in (1, 2) and not conn_config.llm.openrouter_api_key:
                                logger.error("Cloud mode requested but no API key configured")
                                if not ws.closed:
                                    await ws.send_json({
                                        "type": "config_update",
                                        "error": "No OpenRouter API key configured",
                                        "voice_mode": 0,
                                    })
                                continue

                            # Update session system prompt in DB for conversation engine
                            # Chat v4·C (refs #27): also persist voice_mode + llm_model
                            # onto the session row so the drawer surfaces the active
                            # mode fingerprint and pipeline-resume picks the right
                            # backends without a fresh config_update from the client.
                            sid = conn_state.get("session_id")
                            if sid and self._db:
                                # Resolve the best "active model" string to persist,
                                # matching the client-visible payload below.
                                if voice_mode == 2:
                                    active_model_db = conn_config.llm.openrouter_model or ""
                                elif llm_be == "tinkerclaw":
                                    active_model_db = conn_config.llm.tinkerclaw_model or ""
                                elif llm_be == "ollama":
                                    active_model_db = conn_config.llm.ollama_model or ""
                                else:
                                    active_model_db = str(llm_model or "")
                                try:
                                    await self._db.update_session(
                                        sid,
                                        system_prompt=conn_config.llm.system_prompt,
                                        voice_mode=int(voice_mode),
                                        llm_model=active_model_db[:128],
                                    )
                                except Exception:
                                    logger.warning(
                                        "Failed to update session system_prompt / mode"
                                    )

                            # Apply config
                            conn_config.stt.backend = stt_be
                            conn_config.tts.backend = tts_be
                            conn_config.llm.backend = llm_be

                            # Propagate API keys for cloud STT/TTS backends (modes 1-2, or mode 3 with cloud STT)
                            if voice_mode in (1, 2) or (voice_mode == 3 and stt_be == "openrouter"):
                                conn_config.stt.openrouter_api_key = conn_config.llm.openrouter_api_key
                                conn_config.stt.openrouter_url = conn_config.llm.openrouter_url
                                conn_config.tts.openrouter_api_key = conn_config.llm.openrouter_api_key
                                conn_config.tts.openrouter_url = conn_config.llm.openrouter_url

                            # US-P01: Acquire conn_lock to serialize the swap with
                            # stop/text handlers. Without this, start_processing()
                            # could run concurrently with swap_backends().
                            async with conn_lock:
                                # Hot-swap backends on pipeline AND conversation engine
                                pipeline = conn_state.get("pipeline")
                                if pipeline:
                                    try:
                                        # swap_backends() now handles cancel internally
                                        # and sets _swapping flag to drop audio during swap
                                        await pipeline.swap_backends(conn_config)
                                        # Inject session key for TinkerClaw conversation continuity
                                        if llm_be == "tinkerclaw" and hasattr(pipeline, '_llm'):
                                            if hasattr(pipeline._llm, 'set_session_key'):
                                                pipeline._llm.set_session_key(
                                                    conn_state.get("session_id", ""))
                                    except Exception as e:
                                        logger.exception("Backend swap failed")
                                        if not ws.closed:
                                            await ws.send_json({
                                                "type": "config_update",
                                                "error": f"Backend swap failed: {e}",
                                                "voice_mode": 0,
                                            })
                                        continue

                                # Also swap ConversationEngine LLM (used by _handle_text)
                                if self._conversation:
                                    try:
                                        from dragon_voice.llm import create_llm
                                        if self._conversation._llm:
                                            await self._conversation._llm.shutdown()
                                        new_llm = create_llm(conn_config.llm)
                                        await new_llm.initialize()
                                        self._conversation._llm = new_llm
                                        logger.info("ConversationEngine LLM swapped to %s", new_llm.name)
                                    except Exception as e:
                                        logger.exception("ConversationEngine LLM swap failed: %s", e)

                            # Update displayed names
                            self._stt_name = stt_be
                            self._tts_name = tts_be
                            self._llm_name = llm_be

                            # Confirm to Tab5
                            if not ws.closed:
                                # Report actual model for any mode
                                if voice_mode == 2:
                                    active_model = conn_config.llm.openrouter_model
                                elif llm_be == "tinkerclaw":
                                    active_model = conn_config.llm.tinkerclaw_model
                                elif llm_be == "ollama":
                                    active_model = conn_config.llm.ollama_model
                                else:
                                    active_model = ""
                                await ws.send_json({
                                    "type": "config_update",
                                    "config": {
                                        "stt": stt_be, "tts": tts_be,
                                        "llm": llm_be,
                                        "llm_model": active_model,
                                        "voice_mode": voice_mode,
                                        "cloud_mode": voice_mode >= 1,
                                    },
                                })

                    elif cmd_type == "config_ack":
                        logger.debug("Connection %s: config_ack %s", ws_id, cmd.get("applied"))

                    else:
                        logger.warning("Unknown command from %s: %s", ws_id, cmd_type)

                elif msg.type == WSMsgType.ERROR:
                    logger.error("WebSocket error for %s: %s", ws_id, ws.exception())
                    break

        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("WebSocket handler error for %s", ws_id)
        finally:
            # Stop keepalive
            _keepalive_running = False
            _keepalive_task.cancel()
            # Clean up: pause session, mark device offline, shut down pipeline
            await self._handle_disconnect(conn_state)
            self._active_connections.pop(ws_id, None)
            logger.info("WebSocket disconnected: %s (ws_id=%s)", peer, ws_id)

        return ws

    async def _handle_register(
        self,
        ws: web.WebSocketResponse,
        conn_state: dict,
        cmd: dict,
        on_audio,
        on_event,
        conn_config: VoiceConfig,
    ) -> None:
        """Handle device registration message."""
        device_id = cmd.get("device_id", "")
        hardware_id = cmd.get("hardware_id", "")
        requested_session = cmd.get("session_id")

        if not device_id:
            await ws.send_json({"type": "error", "code": "session_invalid",
                                "message": "device_id is required"})
            return

        ws_id = conn_state["ws_id"]
        logger.info("Registering device %s (hw=%s) on connection %s", device_id, hardware_id, ws_id)

        # P13: Evict stale connections for the same device_id.
        # Race condition: new connection arrives before aiohttp detects old TCP close.
        # The old keepalive task is still running, and its pipeline isn't shut down yet.
        for old_ws_id, old_conn in list(self._active_connections.items()):
            if old_ws_id == ws_id:
                continue  # Skip ourselves
            if old_conn.get("device_id") == device_id and old_conn.get("registered"):
                logger.warning(
                    "P13: Device %s already has connection %s — evicting stale connection",
                    device_id, old_ws_id,
                )
                # Shut down the old pipeline
                old_pipeline = old_conn.get("pipeline")
                if old_pipeline:
                    try:
                        await old_pipeline.shutdown()
                    except Exception as e:
                        logger.warning("P13: old pipeline shutdown failed for %s: %s", old_ws_id, e)
                    old_conn["pipeline"] = None

                # Pause the old session (not end — it might be resumed by the new connection)
                old_sid = old_conn.get("session_id")
                if old_sid and self._session_mgr:
                    await self._session_mgr.pause_session(old_sid)

                # Mark as unregistered so _handle_disconnect won't mark device offline
                old_conn["registered"] = False

                # Remove from active connections — _handle_disconnect will be a no-op
                self._active_connections.pop(old_ws_id, None)

                logger.info("P13: Evicted stale connection %s for device %s", old_ws_id, device_id)

        # Upsert device in DB
        await self._db.upsert_device(
            device_id=device_id,
            hardware_id=hardware_id,
            name=cmd.get("name", ""),
            firmware_ver=cmd.get("firmware_ver", ""),
            platform=cmd.get("platform", ""),
            capabilities=cmd.get("capabilities"),
        )
        await self._db.add_event(
            "device.connected", device_id=device_id,
            data={"platform": cmd.get("platform", ""), "firmware_ver": cmd.get("firmware_ver", "")}
        )

        # Get or create session
        session, resumed = await self._session_mgr.get_or_create_session(
            device_id=device_id,
            requested_session_id=requested_session,
            system_prompt=conn_config.llm.system_prompt,
        )
        session_id = session["id"]

        # Update connection state FIRST (before slow pipeline init)
        conn_state["session_id"] = session_id
        conn_state["device_id"] = device_id
        conn_state["registered"] = True
        conn_state["response_mode"] = "always_speak"  # voice device gets TTS

        # Store tool event callbacks per-connection (NOT on shared conversation engine)
        if self._tool_registry:
            async def _on_tool_call(call):
                if not ws.closed:
                    await ws.send_json({"type": "tool_call", "tool": call["tool"], "args": call["args"]})

            async def _on_tool_result(result):
                if not ws.closed:
                    await ws.send_json({"type": "tool_result", **result})

            conn_state["on_tool_call"] = _on_tool_call
            conn_state["on_tool_result"] = _on_tool_result

        # Send session_start IMMEDIATELY — before slow pipeline init
        # Tab5 will timeout if we don't respond quickly
        try:
            await ws.send_json({
                "type": "session_start",
                "session_id": session_id,
                "device_id": device_id,
                "resumed": resumed,
                "message_count": session.get("message_count", 0),
                "config": {
                    "stt": conn_config.stt.backend,
                    "tts": conn_config.tts.backend,
                    "llm": conn_config.llm.backend,
                    "tts_sample_rate": conn_config.audio.input_sample_rate,
                    "response_mode": "match_input",
                    "system_prompt": conn_config.llm.system_prompt,
                },
            })
        except Exception as e:
            logger.warning("Failed to send session_start to %s: %s (client may have disconnected)", ws_id, e)
            return

        logger.info(
            "Device %s registered on session %s (resumed=%s, ws_id=%s)",
            device_id, session_id, resumed, ws_id,
        )

        # Reset conn_config to local defaults before pipeline init.
        # Tab5 will immediately send config_update with its actual mode,
        # so this avoids initializing cloud backends only to swap them out.
        conn_config.stt.backend = "moonshine"
        conn_config.tts.backend = "piper"
        conn_config.llm.backend = conn_config.llm.local_backend or "ollama"
        conn_config.llm.system_prompt = SYSTEM_PROMPT_LOCAL
        conn_config.llm.max_tokens = MAX_TOKENS_LOCAL

        # NOW initialize the voice pipeline (slow: Moonshine load ~2s)
        # This happens AFTER session_start is sent so Tab5 doesn't timeout
        pipeline = VoicePipeline(
            conn_config, on_audio, on_event,
            conversation_engine=self._conversation,
            session_id=session_id,
            media_pipeline=self._media_pipeline,
        )
        try:
            await pipeline.initialize()
        except Exception as e:
            logger.exception("Failed to initialize pipeline for %s", ws_id)
            if not ws.closed:
                await ws.send_json({"type": "error", "code": "internal",
                                    "message": f"Pipeline init failed: {e}"})
            return

        conn_state["pipeline"] = pipeline
        logger.info("Pipeline ready for %s", ws_id)

    async def _handle_text(
        self, ws: web.WebSocketResponse, conn_state: dict, cmd: dict
    ) -> None:
        """Handle text input message — goes directly to conversation engine."""
        session_id = conn_state.get("session_id")
        if not session_id or not self._conversation:
            await ws.send_json({"type": "error", "code": "session_invalid",
                                "message": "Not registered — send register first"})
            return

        content = cmd.get("content", "").strip()
        if not content:
            return

        text = content
        logger.info("Text input on session %s: %s", session_id, text[:80])

        # TinkerClaw mode: bypass ConversationEngine, use ConversationEngine's
        # swapped LLM (not pipeline._llm which may be stale after swap race)
        conn_cfg = conn_state.get("config")
        if conn_cfg and conn_cfg.llm.backend == "tinkerclaw" and self._conversation and self._conversation._llm:
            llm = self._conversation._llm
            logger.info("_handle_text TinkerClaw bypass via ConvEngine LLM: %s", llm.name)
            if hasattr(llm, 'set_session_key'):
                llm.set_session_key(conn_state.get("session_id", ""))

            # Send a "thinking" indicator immediately to keep the WS alive.
            # TinkerClaw agent can take 10-30s before first token (memory recall,
            # skill execution). Without this, ngrok kills the idle connection.
            if not ws.closed:
                await ws.send_json({"type": "llm", "text": ""})

            # Also start a keepalive task that pings every 10s during processing.
            # 5s timeout on each send prevents blocking if the event loop is
            # delayed by inference GIL contention (US-DQ05).
            keepalive_active = True
            async def _keepalive():
                while keepalive_active:
                    await asyncio.sleep(10)
                    if keepalive_active and not ws.closed:
                        try:
                            await asyncio.wait_for(ws.ping(), timeout=5.0)
                        except (asyncio.TimeoutError, Exception):
                            break

            keepalive_task = asyncio.create_task(_keepalive())

            full_response = []
            try:
                async for token in llm.generate_stream_with_messages([
                    {"role": "user", "content": text}
                ]):
                    full_response.append(token)
                    if not ws.closed:
                        await ws.send_json({"type": "llm", "text": token})
            finally:
                keepalive_active = False
                keepalive_task.cancel()

            response_text = "".join(full_response)
            logger.info("TinkerClaw text response (%d chars): %s",
                        len(response_text), response_text[:80])
            if not ws.closed:
                await ws.send_json({"type": "llm_done", "llm_ms": 0, "text": response_text})

            # Rich media detection for TinkerClaw responses too
            if full_response and self._media_pipeline:
                try:
                    media_events = await self._media_pipeline.process_response(
                        response_text, session_id
                    )
                    for event in media_events:
                        if not ws.closed:
                            await ws.send_json(event)
                    if media_events:
                        logger.info("Sent %d media events for TinkerClaw response", len(media_events))
                        cleaned = self._media_pipeline.strip_rendered_content(response_text, media_events)
                        logger.info("Text stripped: %d→%d chars", len(response_text), len(cleaned))
                        if cleaned != response_text and not ws.closed:
                            await ws.send_json({"type": "text_update", "text": cleaned})
                            logger.info("Sent text_update with cleaned text")
                except Exception as e:
                    logger.warning("TinkerClaw media detection failed: %s", e)

            return

        try:
            # Stream LLM response via conversation engine
            full_response = []
            async for token in self._conversation.process_text_stream(
                session_id=session_id,
                text=content,
                input_mode="text",
                on_tool_call=conn_state.get("on_tool_call"),
                on_tool_result=conn_state.get("on_tool_result"),
            ):
                full_response.append(token)
                if not ws.closed:
                    await ws.send_json({"type": "llm", "text": token})

            response_text = "".join(full_response)

            if not ws.closed:
                await ws.send_json({"type": "llm_done", "llm_ms": 0})

            # Rich media detection — scan response for image/chart/map references
            if full_response:
                try:
                    media_events = await self._media_pipeline.process_response(
                        response_text, session_id
                    )
                    for event in media_events:
                        if not ws.closed:
                            await ws.send_json(event)
                    if media_events:
                        cleaned = self._media_pipeline.strip_rendered_content(response_text, media_events)
                        if cleaned != response_text and not ws.closed:
                            await ws.send_json({"type": "text_update", "text": cleaned})
                except Exception as e:
                    logger.warning("Media detection failed: %s", e)

            # Synthesize TTS for the text response (only if response_mode != match_input)
            # match_input = text in, text out. always_speak = always TTS.
            pipeline = conn_state.get("pipeline")
            response_mode = conn_state.get("response_mode", "always_speak")
            if (pipeline and pipeline._tts and response_text.strip()
                    and not ws.closed and response_mode != "match_input"):
                try:
                    await ws.send_json({"type": "tts_start"})
                    t0 = time.monotonic()
                    audio_bytes = await asyncio.wait_for(
                        pipeline._tts.synthesize(response_text), timeout=30
                    )
                    tts_ms = (time.monotonic() - t0) * 1000

                    if audio_bytes:
                        tts_rate = pipeline._tts.sample_rate
                        target_rate = conn_cfg.audio.input_sample_rate if conn_cfg else 16000
                        if tts_rate != target_rate:
                            import numpy as np
                            audio_i16 = np.frombuffer(audio_bytes, dtype=np.int16)
                            ratio = target_rate / tts_rate
                            new_len = int(len(audio_i16) * ratio)
                            indices = np.arange(new_len) / ratio
                            idx_floor = np.clip(indices.astype(np.int32), 0, len(audio_i16) - 2)
                            frac = indices - idx_floor
                            audio_bytes = (audio_i16[idx_floor] * (1 - frac)
                                         + audio_i16[idx_floor + 1] * frac).astype(np.int16).tobytes()

                        chunk_size = 4096
                        pace_sleep = (chunk_size / 2) / target_rate * 0.8
                        for i in range(0, len(audio_bytes), chunk_size):
                            chunk = audio_bytes[i:i + chunk_size]
                            if not ws.closed:
                                await ws.send_bytes(chunk)
                            if i > chunk_size * 3:
                                await asyncio.sleep(pace_sleep)

                    if not ws.closed:
                        await ws.send_json({"type": "tts_end", "tts_ms": round(tts_ms)})
                except Exception:
                    logger.exception("TTS for text input failed")
                    # Always send tts_end so Tab5 doesn't hang in SPEAKING
                    if not ws.closed:
                        await ws.send_json({"type": "tts_end", "tts_ms": 0})

            logger.info("Text response on session %s: %s", session_id, response_text[:80])

        except Exception:
            logger.exception("Text processing error on session %s", session_id)
            if not ws.closed:
                await ws.send_json({"type": "error", "code": "llm_failed",
                                    "message": "Text processing failed"})

    async def _handle_user_media(self, ws, conn_state, cmd):
        """Handle image/audio uploaded by Tab5 for multimodal LLM analysis."""
        import base64
        media_id = cmd.get("media_id", "")
        text = cmd.get("text", "What's in this image?")
        session_id = conn_state.get("session_id", "")
        conn_config = conn_state.get("config", self._config)

        image_path = await self._media_store.get_path(media_id)
        if not image_path:
            if not ws.closed:
                await ws.send_json({"type": "error", "message": "Image not found"})
            return

        backend = conn_config.llm.backend
        if backend == "ollama" and "vision" not in conn_config.llm.ollama_model:
            if not ws.closed:
                await ws.send_json({
                    "type": "error",
                    "message": "Image analysis needs Cloud or TinkerClaw mode"
                })
            return

        with open(image_path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode()

        messages = [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                {"type": "text", "text": text},
            ]
        }]

        full_response = []
        llm = conn_state.get("conversation")
        if llm and hasattr(llm, '_llm'):
            llm_backend = llm._llm
        else:
            llm_backend = None

        if not llm_backend:
            if not ws.closed:
                await ws.send_json({"type": "error", "message": "No LLM available"})
            return

        try:
            async for token in llm_backend.generate_stream_with_messages(messages):
                full_response.append(token)
                if not ws.closed:
                    await ws.send_json({"type": "llm", "text": token})
        except Exception as e:
            logger.error("user_media LLM failed: %s", e)
            if not ws.closed:
                await ws.send_json({"type": "error", "message": str(e)})
            return

        if not ws.closed:
            await ws.send_json({"type": "llm_done", "llm_ms": 0})

        if full_response and self._message_store:
            try:
                await self._message_store.add_message(
                    session_id=session_id,
                    role="assistant",
                    content="".join(full_response),
                    input_mode="vision",
                )
            except Exception as e:
                logger.warning("Failed to store vision response: %s", e)

    async def _handle_disconnect(self, conn_state: dict) -> None:
        """Handle WebSocket disconnect: pause session, mark device offline."""
        session_id = conn_state.get("session_id")
        device_id = conn_state.get("device_id")
        ws_id = conn_state.get("ws_id")
        pipeline = conn_state.get("pipeline")

        try:
            # Pause session (not end — it can be resumed)
            if session_id and self._session_mgr:
                await self._session_mgr.pause_session(session_id)

            # Mark device offline ONLY if no other active connection for same device.
            if device_id and self._db:
                other_active = any(
                    c.get("device_id") == device_id and c.get("registered")
                    for cid, c in self._active_connections.items()
                    if cid != ws_id
                )
                if not other_active:
                    await self._db.set_device_online(device_id, False)
                    await self._db.add_event(
                        "device.disconnected", device_id=device_id,
                        data={"session_id": session_id},
                    )
        except (RuntimeError, Exception) as e:
            # Database may be closed during server shutdown — safe to ignore
            logger.debug("_handle_disconnect db access failed (shutdown?): %s", e)

        # Shut down pipeline
        if pipeline:
            await pipeline.shutdown()


def run_server(config: VoiceConfig) -> None:
    """Start the voice server (blocking)."""
    server = VoiceServer(config)
    app = server.create_app()

    logger.info(
        "Starting Dragon Voice Server on %s:%d",
        config.server.host,
        config.server.port,
    )

    web.run_app(
        app,
        host=config.server.host,
        port=config.server.port,
        print=lambda msg: logger.info(msg),
    )
