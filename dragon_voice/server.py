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
from typing import ClassVar, Optional

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

        # Media handling (rich media detection + user image uploads).
        # Wave 14 W14-H04: construct the URL signer from server.api_token
        # so /api/media/{id} URLs are HMAC-signed + time-bounded.  Falls
        # back to unsigned URLs when api_token is blank (dev bootstrap).
        from dragon_voice.media.url_signer import MediaUrlSigner
        self._media_url_signer = MediaUrlSigner(
            secret=getattr(self._config.server, "api_token", "") or ""
        )
        self._media_store = MediaStore()
        self._media_pipeline = MediaPipeline(
            self._media_store, url_signer=self._media_url_signer
        )
        self._media_cleanup_task: Optional[asyncio.Task] = None

        # Wave 15 W15-C01: shared backend pool so STT/TTS/LLM instances
        # persist across Tab5 WS reconnects.  Key = stable signature
        # tuple (see pipeline._stt_sig / _tts_sig / _llm_sig).  Every
        # VoicePipeline takes this dict and borrows/returns backends.
        # Backends are only shut down on server shutdown; per-pipeline
        # shutdown is a no-op for pooled instances.
        self._backend_pool: dict = {}

        # Wave 15 W15-H01 + W15-H06: rate-limit bucket store.  Keyed on
        # (client_ip, method, path) tuple; value is
        # {"window_start": float, "count": int}.  See
        # `_rate_limit_middleware` for semantics.
        self._rate_buckets: dict[tuple, dict] = {}

    def create_app(self) -> web.Application:
        """Create and configure the aiohttp application."""
        app = web.Application(
            client_max_size=32 * 1024 * 1024,  # 32MB for audio uploads
            # Wave 14 W14-M06: _security_headers_middleware is OUTERMOST
            # (first in the list) so every response — including the 401
            # from _auth_middleware before handler even runs — gets the
            # defensive headers stamped on its way out.  CORS runs next
            # and can't override because the security middleware uses
            # `if k not in response.headers` semantics.
            middlewares=[
                self._security_headers_middleware,
                self._cors_middleware,
                self._auth_middleware,
                # Wave 15 W15-H01 + W15-H06: rate limiter runs AFTER auth
                # so only authenticated clients count against the budget
                # — unauthenticated hits are already rejected cheaply.
                self._rate_limit_middleware,
            ],
        )

        # HTTP routes (legacy)
        app.router.add_get("/", self._handle_status)
        app.router.add_get("/health", self._handle_health)
        app.router.add_post("/debug/widget_chart", self._debug_widget_chart)
        app.router.add_post("/debug/widget_prompt", self._debug_widget_prompt)
        app.router.add_post("/debug/widget_card", self._debug_widget_card)
        app.router.add_post("/debug/widget_media", self._debug_widget_media)
        app.router.add_get("/api/config", self._handle_get_config)
        app.router.add_post("/api/config", self._handle_set_config)

        # Wave 15 W15-C01: tracemalloc-backed mem-diff probe.  Returns
        # top-N allocation growers since baseline so we can pinpoint
        # the RSS leak.  Bearer-gated via the auth middleware.
        app.router.add_get("/debug/mem", self._handle_debug_mem)

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
    # Wave 14 W14-M14: frozenset at class scope (ruff RUF012) — no
    # accidental mutation across instances.
    _CORS_ALLOWED_ORIGINS: ClassVar[frozenset[str]] = frozenset({
        "http://localhost:3500",
        "http://127.0.0.1:3500",
        "http://192.168.1.90:8080",
        "https://tinkerclaw-dashboard.ngrok.dev",
    })

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
                "Access-Control-Allow-Headers": "Content-Type, X-Sample-Rate, Accept, Authorization",
                "Access-Control-Max-Age": "3600",
            })
        response = await handler(request)
        response.headers["Access-Control-Allow-Origin"] = origin
        return response

    # ----------------------------------------------------------------- Security headers
    #
    # Wave 14 W14-M06: stamp the standard defensive headers on every
    # response. Amplifies the W14-H02 dashboard XSS sweep (CSP blocks
    # the payload even if a new innerHTML site slips through the
    # escHtml gate) and blocks framing + MIME sniffing for free.
    # /dashboard SPA uses Google Fonts so the CSP allows that origin
    # specifically; all other sources stay same-origin.
    _SECURITY_HEADERS = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        # CSP — intentionally strict.  style-src allows 'unsafe-inline'
        # because the dashboard inlines its whole stylesheet.  If we
        # ever extract the CSS to a separate file, tighten this.
        "Content-Security-Policy": (
            "default-src 'self'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'"
        ),
    }

    @web.middleware
    async def _security_headers_middleware(self, request: web.Request, handler):
        response = await handler(request)
        # Only stamp on Response objects — WS upgrade responses are
        # not subject to CSP and setting these headers on them confuses
        # some clients. aiohttp WebSocketResponse doesn't expose
        # .headers until prepare(); the middleware path doesn't touch
        # it in that case.
        try:
            for k, v in self._SECURITY_HEADERS.items():
                if k not in response.headers:
                    response.headers[k] = v
        except (AttributeError, TypeError):
            # WS responses or streaming responses that don't expose
            # headers in this phase — skip silently.
            pass
        return response

    # ----------------------------------------------------------------- Auth
    #
    # Wave 13 C2 security: bearer-token auth on every privileged route.
    # Matches the scheme docs/protocol.md calls out for Tab5 debug server;
    # the token is read from config.yaml (reads DRAGON_API_TOKEN env)
    # and can be rotated without code changes.  Public paths are allowlisted
    # (health, WS handshake is separately gated via the `register` frame).
    _AUTH_PUBLIC_PREFIXES = (
        "/health",              # liveness probe
        # Wave 14 W14-C04: /ws/voice is allow-listed here (because the
        # bearer comes from the upgrade header, not the request path),
        # but _handle_ws_voice itself performs the Authorization check
        # before ws.prepare().  Do NOT remove /ws/voice from this list or
        # the CORS middleware will bounce the upgrade.
        "/ws/voice",            # auth enforced inside _handle_ws_voice
        "/dashboard",           # proxied to localhost:3500 (separately gated)
        # Wave 14 W14-H04: /api/media/* remains in the public allowlist
        # (so Tab5 doesn't need to present the bearer on image fetches),
        # but the handler itself now requires an HMAC signature in the
        # query string when server.api_token is configured.  The URLs
        # emitted in WS media events carry that signature automatically.
        "/api/media/",
    )

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        path = request.path or "/"
        # Never gate OPTIONS (CORS middleware already answered it).
        if request.method == "OPTIONS":
            return await handler(request)
        if any(path == p or path.startswith(p) for p in self._AUTH_PUBLIC_PREFIXES):
            return await handler(request)
        # Resolve the expected token once from config (hot-reload-safe).
        expected = (getattr(self._config.server, "api_token", "") or "").strip()
        if not expected:
            # Token not configured — fail closed on private paths to avoid
            # accidentally exposing everything when a deployer forgets.
            # During local-dev bootstrap, set `api_token: ""` in config.yaml
            # explicitly to an empty-string marker + allowlist the paths.
            return web.json_response(
                {"error": "dragon_api_token_not_configured",
                 "message": "Set DRAGON_API_TOKEN in env or api_token in config.yaml"},
                status=503)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return web.json_response(
                {"error": "missing_bearer_token",
                 "message": "Authorization: Bearer <token> required"},
                status=401)
        supplied = auth[7:].strip()
        # Constant-time compare to avoid timing-oracle credential leaks.
        import hmac
        if not hmac.compare_digest(supplied, expected):
            return web.json_response(
                {"error": "invalid_bearer_token",
                 "message": "token rejected"}, status=401)
        return await handler(request)

    # --------------------------------------------------------------- Rate limit

    # Wave 15 W15-H01 + W15-H06: throttle per-IP + per-path on a small
    # fixed window.  Keyed on client-IP + method + path so a DELETE to
    # /api/v1/devices/foo burns budget independently from a POST to
    # /api/v1/sessions/bar/chat.  State-changing + SSE-streaming
    # endpoints get tight limits; read-only endpoints are
    # unthrottled.  In-memory only — if the process restarts the
    # counters reset, which is fine for this threat model (single-
    # tenant deployment, we just want to stop loop-bugs and obvious
    # DoS).
    _RATE_LIMIT_RULES: ClassVar[tuple[tuple[str, str, int, int], ...]] = (
        # (method_prefix, path_prefix, max_requests, window_seconds)
        ("DELETE", "/api/v1/devices/",          20, 60),
        ("DELETE", "/api/v1/sessions/",         20, 60),
        ("DELETE", "/api/v1/messages",          20, 60),
        ("DELETE", "/api/v1/memory/",           30, 60),
        ("DELETE", "/api/v1/documents/",        10, 60),
        ("DELETE", "/api/v1/config/",           20, 60),
        ("POST",   "/api/v1/sessions/",         30, 60),  # /end, /pause, /resume
        # W15-H06: SSE reconnect amplification — a broken client can
        # re-open the chat stream 30 times/sec on tab-flap.  Cap at
        # 20/min per IP + session to give the client room for legit
        # retries but stop the storm.
        ("POST",   "/api/v1/sessions/",         20, 60),  # covers /chat too
        # Upload path — protect against the 10 MB upload × spam DoS
        ("POST",   "/api/media/upload",         30, 60),
    )

    @web.middleware
    async def _rate_limit_middleware(self, request: web.Request, handler):
        # Fast path: only even consider paths that match at least one rule.
        path = request.path
        method = request.method
        # `match_prefix` just walks the rule table — tiny list so this
        # is cheap even at 1000 req/sec.
        matched = None
        for m, p, cap, win in self._RATE_LIMIT_RULES:
            if method == m and path.startswith(p):
                # Keep the TIGHTEST matching rule (smallest cap).
                if matched is None or cap < matched[0]:
                    matched = (cap, win)
        if matched is None:
            return await handler(request)

        # Use peername as the client key; behind ngrok/proxy it's the
        # proxy IP which is fine for single-tenant threat model.
        peer = request.transport.get_extra_info("peername") if request.transport else None
        client = peer[0] if peer else "unknown"
        cap, win = matched
        key = (client, method, path)

        now = time.monotonic()
        bucket = self._rate_buckets.get(key)
        if bucket is None or now - bucket["window_start"] >= win:
            self._rate_buckets[key] = {"window_start": now, "count": 1}
        else:
            bucket["count"] += 1
            if bucket["count"] > cap:
                retry_after = int(win - (now - bucket["window_start"])) + 1
                logger.warning(
                    "rate-limit: %s %s from %s (%d/%d in %ds)",
                    method, path, client, bucket["count"], cap, win,
                )
                return web.json_response(
                    {
                        "error": "rate_limited",
                        "message": f"limit {cap} requests per {win} s for this path",
                        "retry_after_seconds": retry_after,
                    },
                    status=429,
                    headers={"Retry-After": str(retry_after)},
                )

        # Periodic cheap GC so the dict doesn't grow unbounded over
        # long uptimes (each entry is ~200 B; 10 000 entries = 2 MB).
        if len(self._rate_buckets) > 4096:
            cutoff = now - 600  # anything untouched 10 min gets dropped
            self._rate_buckets = {
                k: v for k, v in self._rate_buckets.items()
                if v["window_start"] > cutoff
            }

        return await handler(request)

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
            from dragon_voice.tools.memory_tools import (
                StoreFactTool, RecallFactsTool, ForgetFactTool,
            )
            self._tool_registry.register(StoreFactTool(self._memory_service))
            self._tool_registry.register(RecallFactsTool(self._memory_service))
            # v4·D Gauntlet G9: two-step confirm-gated forget_fact tool.
            self._tool_registry.register(ForgetFactTool(self._memory_service))

            # Tier 1 tools.
            # Audit D8/K7 dedup (wave 7, 2026-04-20): TimerTool is no
            # longer registered. TimesenseTool (registered below, after
            # SurfaceManager init) covers the "set a timer" use case
            # AND emits widget_live progress.  Keeping both caused the
            # LLM to pick TimerTool on short phrases ("timer 5 min"),
            # leaving the whole widget-platform reference flow unreachable
            # from voice.  TimerTool class file is retained for REST-only
            # use cases; it's just not wired into the agentic loop.
            from dragon_voice.tools.weather_tool import WeatherTool
            from dragon_voice.tools.calculator_tool import CalculatorTool
            from dragon_voice.tools.unit_converter_tool import UnitConverterTool
            from dragon_voice.tools.note_tool import NoteTool
            from dragon_voice.tools.system_tool import SystemInfoTool
            from dragon_voice.tools.stock_ticker_tool import StockTickerTool

            self._tool_registry.register(WeatherTool())
            self._tool_registry.register(CalculatorTool())
            self._tool_registry.register(UnitConverterTool())
            self._tool_registry.register(SystemInfoTool())
            self._tool_registry.register(StockTickerTool())

            logger.info("Agentic modules initialized (tools: %d, memory: ok)",
                        len(self._tool_registry.list_tools()))
        except Exception as e:
            logger.warning("Agentic modules not available: %s", e)

        # v4·D Phase 4g stability fix (audit P0 #1): instantiate the
        # SurfaceManager so Tab5 widget_action events have somewhere to
        # land.  Previously server.py imported nothing from surfaces/,
        # and every Tab5 widget tap logged "Unknown command" and died.
        from dragon_voice.surfaces import SurfaceManager
        self._surface_mgr = SurfaceManager()
        logger.info("SurfaceManager initialized")
        # Audit P0 #1 (2026-04-20): register TimesenseTool - the reference
        # widget-emitting skill.  Must be registered AFTER surface_mgr init
        # (it takes the surface manager as constructor arg).  TimerTool and
        # TimesenseTool have distinct names (timer vs timesense_timer) so
        # both coexist; LLM picks based on description.
        if self._tool_registry is not None:
            try:
                from dragon_voice.tools.timesense_tool import TimesenseTool
                self._tool_registry.register(TimesenseTool(self._surface_mgr))
                logger.info("TimesenseTool registered (widget emitter)")
                # Wave 12 skill SDK: QuickPollTool is the reference
                # for docs/SKILL_AUTHORING.md — declarative
                # surface.prompt(on_action=handler), no imperative
                # register_action bookkeeping.  ~80 LOC total, a
                # minimal viable widget skill.
                from dragon_voice.tools.quick_poll_tool import QuickPollTool
                self._tool_registry.register(QuickPollTool(self._surface_mgr))
                logger.info("QuickPollTool registered (declarative widget skill)")
            except Exception as e:
                logger.warning("TimesenseTool registration failed: %s", e)


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
            media_url_signer=self._media_url_signer,
        )

        # Notes API routes
        try:
            from dragon_voice.notes.db import NotesDB
            from dragon_voice.notes.service import NotesService
            from dragon_voice.notes.api import setup_routes as setup_notes_routes

            notes_db = NotesDB()
            # Wave 14 W14-C05: NotesDB.initialize is async now. The prior
            # direct call here was un-awaited, producing a stray coroutine
            # and an intermittent None-connection race. NotesService.initialize
            # already calls await self._db.initialize(), so this line is gone.
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
                                    backend_pool=self._backend_pool,
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
            # Wave 14 W14-M09 pattern: await so we don't leak the task
            # across loop shutdown (produces the same "Task was
            # destroyed but it is pending" warning wave-13 H3 fixed).
            try:
                await self._memory_monitor_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._purge_task and not self._purge_task.done():
            self._purge_task.cancel()
            # Wave 14 W14-M09: _periodic_purge sleeps 86400 s in one
            # shot; cancel-without-await left it attached long enough
            # for aiohttp to close the loop first, producing
            # "RuntimeError: Event loop is closed" at systemctl restart.
            try:
                await self._purge_task
            except (asyncio.CancelledError, Exception):
                pass
        # Wave 13 H3: the media cleanup loop was being left running on shutdown
        # because it isn't touched here. If it was mid-sleep when the event loop
        # closes, asyncio logs "Task was destroyed but it is pending" warnings
        # and the 24h TTL cleaner can orphan partial deletes. Cancel + await.
        if self._media_cleanup_task and not self._media_cleanup_task.done():
            self._media_cleanup_task.cancel()
            try:
                await self._media_cleanup_task
            except (asyncio.CancelledError, Exception):
                pass

        # Shut down pipelines
        tasks = []
        for _ws_id, conn in list(self._active_connections.items()):
            pipeline = conn.get("pipeline")
            if pipeline:
                tasks.append(pipeline.shutdown())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active_connections.clear()

        # Wave 15 W15-C01: now that no pipelines are holding references,
        # shut down the shared backend pool.  Per-pipeline shutdowns
        # above are no-ops for pooled backends (_pooled_* flag), so the
        # pool is the single owner responsible for final teardown.
        pool_tasks = []
        for key, backend in list(self._backend_pool.items()):
            logger.info("W15-C01: releasing pooled backend %s", key)
            try:
                pool_tasks.append(backend.shutdown())
            except (AttributeError, RuntimeError) as e:
                logger.warning("W15-C01: pool backend %s shutdown raised: %s", key, e)
        if pool_tasks:
            await asyncio.gather(*pool_tasks, return_exceptions=True)
        self._backend_pool.clear()

        # v4·D audit P0 fix: release the inference ThreadPoolExecutor so
        # uvloop can exit cleanly.  Previously zombie threads lingered.
        try:
            from dragon_voice.pipeline import shutdown_inference_executor
            shutdown_inference_executor(wait=False)
            logger.info("Inference executor shut down")
        except Exception:
            logger.debug("inference executor shutdown failed", exc_info=True)

        # Close shared proxy session (DQ08)
        if self._proxy_session and not self._proxy_session.closed:
            await self._proxy_session.close()

        # Shut down foundation
        if self._notes_svc:
            await self._notes_svc.shutdown()
        if self._media_pipeline:
            # Wave 14 W14-H12: close the MediaPipeline's shared aiohttp
            # ClientSession.  Prior behaviour left it dangling across
            # systemctl restart, logging "Unclosed client session" and
            # making the wave-13 FD counter noisy.
            try:
                await self._media_pipeline.close()
            except Exception:
                logger.debug("MediaPipeline shutdown failed", exc_info=True)
        if self._memory_service:
            logger.info("Shutting down memory service")
            # Wave 14 W14-H11: close the shared Ollama HTTP session.
            try:
                await self._memory_service.shutdown()
            except Exception:
                logger.debug("MemoryService shutdown raised", exc_info=True)
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

    async def _handle_debug_mem(self, request: web.Request) -> web.Response:
        """W15-C01 mem-diff probe.

        Returns the top-N allocation groups (ranked by growth since
        baseline) from tracemalloc.  The baseline is taken on first
        call; subsequent calls diff against the stored baseline.  The
        ``?reset=1`` query flag replaces the baseline with the current
        snapshot, so we can bisect leak growth windows.

        Requires ``DRAGON_TRACEMALLOC=1`` in the env — otherwise the
        endpoint returns 503 with a hint.
        """
        import tracemalloc
        import resource
        if not tracemalloc.is_tracing():
            return web.json_response(
                {
                    "error": "tracemalloc_disabled",
                    "hint": "export DRAGON_TRACEMALLOC=1 and restart voice service",
                },
                status=503,
            )
        snap = tracemalloc.take_snapshot()
        # Filter out tracemalloc's own frames so the output isn't
        # dominated by bookkeeping.
        snap = snap.filter_traces(
            (
                tracemalloc.Filter(False, tracemalloc.__file__),
                tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
                tracemalloc.Filter(False, "<frozen importlib._bootstrap_external>"),
            )
        )
        reset = request.query.get("reset", "0") == "1"
        topn = int(request.query.get("n", "25"))
        # Current RSS (kilobytes on Linux -> bytes).
        rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss_mb = rss_kb / 1024.0
        out: dict = {
            "rss_mb": round(rss_mb, 1),
            "uptime_seconds": round(time.time() - self._start_time, 1),
            "active_connections": len(self._active_connections),
            "tracemalloc_peak_mb": round(tracemalloc.get_traced_memory()[1] / 1024 / 1024, 1),
        }
        # Optional ?trim=1 to force gc+malloc_trim before snapshot —
        # tells us how much of RSS is reclaimable vs truly leaked.
        if request.query.get("trim", "0") == "1":
            before_rss = rss_mb
            import ctypes
            gc.collect()
            try:
                libc = ctypes.CDLL("libc.so.6")
                libc.malloc_trim(0)
            except (OSError, AttributeError) as e:
                out["malloc_trim_error"] = str(e)
            rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            rss_mb = rss_kb / 1024.0
            out["rss_mb_after_trim"] = round(rss_mb, 1)
            out["rss_reclaimed_mb"] = round(before_rss - rss_mb, 1)

        baseline = getattr(self, "_mem_baseline", None)
        if baseline is None or reset:
            self._mem_baseline = snap
            self._mem_baseline_rss_mb = rss_mb
            out["note"] = "baseline set — call again after load to see diff"
        else:
            stats = snap.compare_to(baseline, "lineno")
            out["rss_delta_mb"] = round(rss_mb - self._mem_baseline_rss_mb, 1)
            out["top"] = [
                {
                    "file": f"{s.traceback[0].filename.split('/')[-1]}:{s.traceback[0].lineno}",
                    "size_mb": round(s.size / 1024 / 1024, 3),
                    "size_diff_mb": round(s.size_diff / 1024 / 1024, 3),
                    "count": s.count,
                    "count_diff": s.count_diff,
                }
                for s in stats[:topn]
            ]

        # gc object counts by type — catches leaks tracemalloc misses
        # (objects that grow in count, held in long-lived caches).
        from collections import Counter
        type_counts = Counter(type(o).__name__ for o in gc.get_objects())
        # Persist a baseline so we can diff count growth too.
        base_counts = getattr(self, "_mem_type_counts", None)
        if base_counts is None or reset:
            self._mem_type_counts = type_counts
            out["top_types"] = [
                {"type": k, "count": v}
                for k, v in type_counts.most_common(25)
            ]
        else:
            diff = {k: type_counts[k] - base_counts.get(k, 0) for k in type_counts}
            growers = sorted(diff.items(), key=lambda kv: -kv[1])[:25]
            out["top_type_growers"] = [
                {"type": k, "delta": d, "now": type_counts[k]}
                for k, d in growers if d > 0
            ]
        return web.json_response(out)


    async def _debug_widget_chart(self, request: web.Request) -> web.Response:
        """POST /debug/widget_chart -- audit B5 evidence. Emits a chart
        widget on every registered Tab5Surface. Body: {title, values, chart_max}."""
        try:
            data = await request.json()
        except Exception:
            data = {}
        title = data.get("title", "Audit chart")
        values = data.get("values", [3, 7, 12, 9, 15, 18, 22, 16, 11, 8, 14, 20])
        chart_max = float(data.get("chart_max", 0))
        if self._surface_mgr is None:
            return web.json_response({"error": "surface_mgr not ready"}, status=503)
        count = 0
        for sid, state in list(self._surface_mgr._sessions.items()):
            try:
                await state.surface.chart(title=title, values=values, chart_max=chart_max,
                                  skill_id="audit", card_id="audit_chart_" + sid[:6])
                count += 1
            except Exception as e:
                logger.warning("debug chart emit failed for %s: %s", sid, e)
        return web.json_response({"emitted": count, "values": values})

    async def _debug_widget_prompt(self, request: web.Request) -> web.Response:
        """POST /debug/widget_prompt -- audit B6/K3 evidence.
        Emits a widget_prompt on every registered Tab5Surface AND
        registers a handler so tap round-trips back to Dragon.
        Body: {title, body, choices: [[text, event], ...]}."""
        try:
            data = await request.json()
        except Exception:
            data = {}
        title = data.get("title", "Confirm?")
        body = data.get("body", "")
        choices_raw = data.get("choices", [["Yes", "audit_yes"], ["No", "audit_no"]])
        choices = []
        for c in choices_raw[:3]:
            if isinstance(c, (list, tuple)) and len(c) >= 2:
                choices.append((str(c[0]), str(c[1])))
        if self._surface_mgr is None:
            return web.json_response({"error": "surface_mgr not ready"}, status=503)
        count = 0
        for sid, state in list(self._surface_mgr._sessions.items()):
            try:
                card_id = f"audit_prompt_{sid[:6]}"
                async def _handle(event: str, payload: dict, _sid=sid, _cid=card_id) -> None:
                    logger.info("audit widget_prompt tapped: session=%s event=%s payload=%s",
                                _sid, event, payload)
                    # Dismiss the card so the tap has an observable effect.
                    try:
                        await state.surface.dismiss(_cid)
                    except Exception:
                        pass
                    self._surface_mgr.unregister_action(_sid, _cid)
                self._surface_mgr.register_action(sid, card_id, _handle)
                await state.surface.prompt(
                    title=title, body=body, choices=choices,
                    skill_id="audit", card_id=card_id,
                )
                count += 1
            except Exception as e:
                logger.warning("debug prompt emit failed for %s: %s", sid, e)
        return web.json_response({"emitted": count, "choices": choices})

    async def _debug_widget_card(self, request: web.Request) -> web.Response:
        """POST /debug/widget_card -- audit B2 evidence.
        Emits a widget_card on every registered Tab5Surface. Cards go
        to chat (not home). Body: {title, body, tone}."""
        try:
            data = await request.json()
        except Exception:
            data = {}
        title = data.get("title", "Workshop draft ready")
        body = data.get("body", "Two edits queued from yesterday. Review before 10:30.")
        tone = data.get("tone", "info")
        if self._surface_mgr is None:
            return web.json_response({"error": "surface_mgr not ready"}, status=503)
        count = 0
        for sid, state in list(self._surface_mgr._sessions.items()):
            try:
                await state.surface.card(title=title, body=body, tone=tone,
                                          skill_id="audit",
                                          card_id="audit_card_" + sid[:6])
                count += 1
            except Exception as e:
                logger.warning("debug card emit failed for %s: %s", sid, e)
        return web.json_response({"emitted": count})

    async def _debug_widget_media(self, request: web.Request) -> web.Response:
        """POST /debug/widget_media -- audit B5 evidence.
        Emits widget_media pointing at a previously uploaded image.
        Body: {url, width, height, alt}."""
        try:
            data = await request.json()
        except Exception:
            data = {}
        url = data.get("url", "")
        width = int(data.get("width", 480))
        height = int(data.get("height", 300))
        alt = data.get("alt", "Audit media")
        if not url:
            return web.json_response({"error": "url required"}, status=400)
        if self._surface_mgr is None:
            return web.json_response({"error": "surface_mgr not ready"}, status=503)
        count = 0
        for sid, state in list(self._surface_mgr._sessions.items()):
            try:
                await state.surface.media(url=url, alt=alt,
                                          title="Audit media",
                                          skill_id="audit",
                                          card_id="audit_media_" + sid[:6])
                count += 1
            except Exception as e:
                logger.warning("debug media emit failed for %s: %s", sid, e)
        return web.json_response({"emitted": count, "url": url})




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

    @staticmethod
    async def _safe_send_json(ws: web.WebSocketResponse, msg: dict) -> bool:
        """Non-raising ws.send_json — returns True on success.

        aiohttp raises ConnectionResetError (and bare Exception in some
        paths) when the transport is mid-close. Handlers that call
        send_json without guarding see the whole register/event flow
        fail and the session gets paused even though the WS is simply
        about to reconnect (refs #31). Use this helper everywhere.

        Returns False silently if the WS is closed or the send fails;
        callers should treat that as "client will reconnect and we'll
        replay on the next session_start" — not a fatal error.
        """
        if ws.closed:
            return False
        try:
            await ws.send_json(msg)
            return True
        except (ConnectionResetError, RuntimeError, asyncio.CancelledError):
            # aiohttp uses RuntimeError("closing transport") on some paths
            return False
        except Exception as e:
            logger.debug("_safe_send_json suppressed: %s", e)
            return False

    @staticmethod
    async def _safe_send_bytes(ws: web.WebSocketResponse, data: bytes) -> bool:
        """Non-raising ws.send_bytes — returns True on success. Same rationale as _safe_send_json."""
        if ws.closed:
            return False
        try:
            await ws.send_bytes(data)
            return True
        except (ConnectionResetError, RuntimeError, asyncio.CancelledError):
            return False
        except Exception as e:
            logger.debug("_safe_send_bytes suppressed: %s", e)
            return False

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
        # Wave 14 W14-C04: authenticate the WS upgrade request.  Prior to
        # this, /ws/voice was publicly reachable and the `register` frame
        # only required a non-empty device_id — so any attacker with the
        # ngrok URL could impersonate any Tab5, hijack sessions, and burn
        # OpenRouter/TinkerClaw budget.
        #
        # Contract:
        #   - If server.api_token is configured: client MUST present
        #       `Authorization: Bearer <token>` on the upgrade request,
        #       matched with hmac.compare_digest.  Mismatch → 401.
        #   - If server.api_token is blank (unprovisioned/dev): allow the
        #       handshake but log-warn so the operator knows they're
        #       running unauthenticated.  This matches the pattern of
        #       letting first-run bootstraps work without breaking Tab5
        #       flashes mid-upgrade.
        expected_token = (getattr(self._config.server, "api_token", "") or "").strip()
        if expected_token:
            auth_header = request.headers.get("Authorization", "")
            supplied = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
            import hmac as _hmac
            if not supplied or not _hmac.compare_digest(supplied, expected_token):
                logger.warning(
                    "WS /ws/voice: rejecting unauthenticated upgrade from %s (header_present=%s)",
                    request.remote, bool(auth_header))
                return web.Response(text="Unauthorized", status=401)
        else:
            logger.warning(
                "WS /ws/voice: server.api_token not configured — allowing "
                "unauthenticated WS. Set DRAGON_API_TOKEN to enforce.")

        # Reject if at connection limit
        if len(self._active_connections) >= self._max_connections:
            logger.warning("Connection limit reached (%d), rejecting", self._max_connections)
            return web.Response(text="Too many connections", status=503)

        # v4·D connectivity audit -- ROOT CAUSE FIX #3.
        #
        # Enable aiohttp's built-in WS heartbeat at 30 s with a 60 s
        # pong-wait window.  Previously heartbeat=None meant the
        # server never sent WS-level pings -- dead sockets could only
        # be detected by a failed send.  With heartbeat enabled, aiohttp
        # emits a PING every `heartbeat` seconds and closes the
        # connection if the peer hasn't replied within `receive_timeout`.
        # Paired with Tab5's new TCP-level keepalive, both sides now
        # notice a half-open socket in well under 60 s instead of
        # waiting for the app layer to try a write and fail.
        ws = web.WebSocketResponse(
            max_msg_size=10 * 1024 * 1024,
            heartbeat=60.0,
            # 2026-04-23 (#58): 120 → 600 s.  Previously Tab5 WS got dropped
            # mid-turn when TC was running a long agent task — heartbeat
            # PING goes out every 60 s but if the TC response hasn't started
            # streaming within 120 s (normal for MiniMax-M2.5 + tools), the
            # aiohttp server-side receive_timeout would yank the connection
            # and Tab5 would see the flap as "Dragon unreachable".
            receive_timeout=600.0,
            autoping=True,
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

                # NOTE: The old 30s "no client message" silence check was removed.
                # Tab5 migrated to esp_websocket_client (voice.c commit 3af34b0) which
                # uses WS-level PING/PONG control frames at 15s interval. Control
                # frames do NOT update _last_client_msg_time (aiohttp handles them
                # internally and they never surface to the message loop), so the
                # silence check produced a 30-45s false-positive close every cycle.
                # Liveness is now detected by: (1) WS-level ping/pong timeout on
                # Tab5 side (45s), (2) this task's send-failure counter above
                # (3 consecutive send failures), and (3) TCP RST propagation.
                # _last_client_msg_time is left as-is for potential future use.

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
            # Wave 14 W14-C06: per-connection background tasks (e.g.
            # cap_downgrade speak_system, out-of-band TTS) tracked here so
            # _handle_disconnect can cancel them before closing the pipeline.
            "bg_tasks": set(),
        }
        self._active_connections[ws_id] = conn_state

        # Callbacks for the pipeline.  v4·D audit P0 fix: route through
        # the shared _safe_send_* helpers so transient disconnects mid-
        # stream don't raise ConnectionResetError up into the pipeline's
        # tight loop (which used to swallow real exceptions).
        async def on_audio(audio_bytes: bytes) -> None:
            if ws.closed:
                return
            await self._safe_send_bytes(ws, audio_bytes)

        async def on_event(event: dict) -> None:
            if not ws.closed:
                await self._safe_send_json(ws, event)
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
                            # closes #56: create_session returns a single dict,
                            # NOT a (dict, bool) tuple — that's
                            # get_or_create_session.  The old tuple-unpack
                            # raised ValueError and tore down the WS handler,
                            # leaving Tab5 dead after a 'clear' + mode-swap
                            # sequence.  Symptom: Tab5 sat in RECONNECTING and
                            # every /chat returned 'voice not connected' with
                            # a blank chat view.
                            session = await self._session_mgr.create_session(
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
                        # v4·D audit P1: rate-limit config_update to 2/sec/conn.
                        # A buggy skill or trigger-happy test harness could
                        # storm mode swaps that each do heavy backend init.
                        _now_cfg = time.monotonic()
                        _last_cfg = conn_state.get("_last_config_update_ts", 0.0)
                        if _now_cfg - _last_cfg < 0.5:
                            logger.debug("config_update rate-limited on %s", ws_id)
                            continue
                        conn_state["_last_config_update_ts"] = _now_cfg
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
                                    # Audit G5 (2026-04-20): revert to Local so Tab5 doesn't
                                    # sit wedged on mode 3 showing an error. Matches the
                                    # OpenRouter-key-missing path below.
                                    if not ws.closed:
                                        await ws.send_json({
                                            "type": "config_update",
                                            "error": "TinkerClaw gateway is not reachable",
                                            "voice_mode": 0,
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
                            sid = conn_state.get("session_id")
                            if sid and self._db:
                                try:
                                    await self._db.update_session(
                                        sid, system_prompt=conn_config.llm.system_prompt
                                    )
                                except Exception:
                                    logger.warning("Failed to update session system_prompt")

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
                                        logger.exception(
                                            "Backend swap failed for %s",
                                            conn_state.get("ws_id", "?"),
                                        )
                                        # W15-C04: `_safe_send_json` owns the
                                        # closed-check + send atomically and
                                        # doesn't raise if the socket closed
                                        # after our swap started.  The old
                                        # `if not ws.closed: send_json(...)`
                                        # pattern had a TOCTOU window where
                                        # TCP FIN could land between check
                                        # and send and raise inside the
                                        # except handler, hiding the original
                                        # backend-swap error.
                                        await self._safe_send_json(ws, {
                                            "type": "config_update",
                                            "error": f"Backend swap failed: {e}",
                                            "voice_mode": 0,
                                        })
                                        continue

                                # Also swap ConversationEngine LLM (used by _handle_text).
                                # W15-C01: prefer the pooled instance so we don't
                                # re-load Ollama / re-open the aiohttp session on
                                # every config_update.  Only the pipeline owns the
                                # shutdown of a pooled backend.
                                if self._conversation:
                                    try:
                                        from dragon_voice.llm import create_llm
                                        from dragon_voice.pipeline import _llm_sig
                                        new_key = _llm_sig(conn_config.llm)
                                        pooled = self._backend_pool.get(new_key)
                                        old_llm = self._conversation._llm
                                        if pooled is not None:
                                            new_llm = pooled
                                        else:
                                            new_llm = create_llm(conn_config.llm)
                                            await new_llm.initialize()
                                            self._backend_pool[new_key] = new_llm
                                        # Only shutdown the OLD one if nobody in the
                                        # pool references it (i.e. it wasn't pooled).
                                        if old_llm is not None and old_llm not in self._backend_pool.values():
                                            await old_llm.shutdown()
                                        self._conversation._llm = new_llm
                                        # 2026-04-23 (#58): also swap _llm_config so the
                                        # compact-vs-full tool prompt logic in
                                        # ConversationEngine._augment_context_with_tools
                                        # picks the right format for the active backend.
                                        # Without this, cloud-mode agents stayed on the
                                        # top-5 compact tool list and never saw weather,
                                        # stock_ticker, timesense_timer, quick_poll, note,
                                        # system_info, or unit_converter.
                                        self._conversation._llm_config = conn_config.llm
                                        logger.info("ConversationEngine LLM swapped to %s%s (backend=%s)",
                                                    new_llm.name, " (pooled)" if pooled else "", conn_config.llm.backend)
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

                                # v4·D Phase 4b vision capability advertisement.
                                # Tab5's camera screen renders a "VISION · <model>
                                # READY" chip based on this.  A model is
                                # vision-capable when:
                                #   1. cloud mode (2) + OpenRouter model matches
                                #      a known vision id (gpt-4o, sonnet, etc)
                                #   2. OR local mode (0) using ollama and the
                                #      ollama_model name contains "vision"
                                #      or "llava"
                                # Per-frame cost estimates are rough mils-per-
                                # frame for the Tab5 1280x720 capture sent as
                                # a ~60 KB JPEG (= ~1500 image tokens at most
                                # vendors).
                                try:
                                    vm = conn_config.llm.openrouter_model.lower() \
                                        if voice_mode == 2 else ""
                                    om = conn_config.llm.ollama_model.lower() \
                                        if voice_mode == 0 else ""
                                    vision_model = ""
                                    per_frame_mils = 0
                                    if voice_mode == 2:
                                        if "gpt-4o" in vm:
                                            vision_model = active_model
                                            per_frame_mils = 1200  # ~$0.012/frame
                                        elif "sonnet" in vm:
                                            vision_model = active_model
                                            per_frame_mils = 4500  # ~$0.045/frame
                                        elif "haiku" in vm:
                                            # Haiku 3.5 supports vision per OR
                                            vision_model = active_model
                                            per_frame_mils = 400
                                    elif voice_mode == 0:
                                        if "vision" in om or "llava" in om:
                                            vision_model = active_model
                                            per_frame_mils = 0  # local = free
                                    await ws.send_json({
                                        "type":           "vision_capability",
                                        "can_see":        bool(vision_model),
                                        "model":          vision_model,
                                        "per_frame_mils": per_frame_mils,
                                    })
                                except Exception:
                                    logger.exception("vision_capability emit failed")

                            # v4·D Gauntlet G7-F: speak a short alert when the
                            # Tab5 auto-downgrades because the daily cap was
                            # hit.  The Tab5 tags its config_update with
                            # reason="cap_downgrade" so the user hears why
                            # their next turn is free even with the screen off.
                            try:
                                if cmd.get("reason") == "cap_downgrade":
                                    pipeline = conn_state.get("pipeline")
                                    if pipeline and hasattr(pipeline, "speak_system"):
                                        # Wave 14 W14-C06: track the task so
                                        # _handle_disconnect can cancel it if
                                        # the user closes mid-utterance.
                                        bg = conn_state["bg_tasks"]
                                        t = asyncio.create_task(pipeline.speak_system(
                                            "Daily budget cap reached. Switched back to local mode."
                                        ))
                                        bg.add(t)
                                        t.add_done_callback(bg.discard)
                            except Exception:
                                logger.exception("cap_downgrade alert failed")

                    elif cmd_type == "widget_action":
                        # v4·D Phase 4g (audit P0 fix): Tab5 fires this
                        # when the user taps a prompt choice / live action
                        # button / list row.  Before this branch existed,
                        # every interactive widget tap was silently
                        # dropped into the "Unknown command" logger.
                        sid = conn_state.get("session_id")
                        cid = cmd.get("card_id")
                        ev  = cmd.get("event")
                        payload = cmd.get("payload") or {}
                        logger.info("widget_action: session=%s card=%s event=%s",
                                    sid, cid, ev)
                        if sid and cid and ev and self._surface_mgr is not None:
                            try:
                                await self._surface_mgr.handle_action(
                                    sid, cid, ev, payload,
                                )
                            except Exception:
                                logger.exception("widget_action dispatch failed")

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

        # v4·D audit P0 fix: expose the widget subset of client capabilities
        # on conn_state so skills can pull it via SurfaceManager and
        # downgrade emissions (smaller lists, lower-res media) for low-end
        # clients.  The register frame already ships this under
        # capabilities.widgets; pluck it out for quick lookup.
        caps = cmd.get("capabilities") or {}
        widget_caps = caps.get("widgets") if isinstance(caps, dict) else None
        conn_state["widget_capabilities"] = widget_caps or {
            "types": ["live", "card"],
            "list_max_items": 3, "chart_max_points": 8,
            "prompt_max_choices": 2,
        }
        logger.info("widget_capabilities for %s: %s",
                    device_id, conn_state["widget_capabilities"])
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

        # v4·D Phase 4g: register this connection's surface with the
        # shared SurfaceManager.  Skills dispatch widget_* emissions
        # through here and widget_action events route back via
        # handle_action().
        # v4·D audit P1: route surface + tool-event sends through the
        # _safe_send_json helper so a transient close mid-widget-emit
        # doesn't bubble into the WS handler and tear the session down.
        if self._surface_mgr is not None:
            async def _surface_send(msg: dict):
                if not ws.closed:
                    await self._safe_send_json(ws, msg)
            await self._surface_mgr.register_session(session_id, _surface_send, caps=conn_state.get("widget_capabilities"))

        # Store tool event callbacks per-connection (NOT on shared conversation engine)
        if self._tool_registry:
            async def _on_tool_call(call):
                if not ws.closed:
                    await self._safe_send_json(ws, {
                        "type": "tool_call",
                        "tool": call["tool"],
                        "args": call["args"],
                    })

            async def _on_tool_result(result):
                if ws.closed:
                    return
                await ws.send_json({"type": "tool_result", **result})
                # v4·D Phase 4c: auto-emit widget_list for web_search results
                # so the Tab5 home live-slot surfaces the top hits without
                # the LLM having to orchestrate a widget call itself.
                try:
                    if result.get("tool") == "web_search":
                        payload = result.get("result") or {}
                        hits = payload.get("results") or []
                        query = payload.get("query", "")
                        items = []
                        for r in hits[:5]:
                            t = str(r.get("title") or r.get("snippet") or "")[:79]
                            if not t:
                                continue
                            items.append({"text": t, "value": ""})
                        if items:
                            await ws.send_json({
                                "type": "widget_list",
                                "skill_id": "web_search",
                                "card_id": f"ws_{session_id[:8]}",
                                "title": (query[:60] or "Web results"),
                                "tone": "info",
                                "priority": 70,
                                "items": items,
                            })
                except Exception:
                    logger.debug("widget_list auto-emit failed", exc_info=True)

            conn_state["on_tool_call"] = _on_tool_call
            conn_state["on_tool_result"] = _on_tool_result

        # Send session_start IMMEDIATELY — before slow pipeline init.
        # Use _safe_send_json so a transient transport close (the Tab5
        # register-before-receive-task-running race, refs #31 + TT #76)
        # doesn't propagate an exception up to the WS handler and force
        # a session pause. If the send drops, the client will reconnect
        # shortly and we'll replay session_start on the next handshake.
        if not await self._safe_send_json(ws, {
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
        }):
            logger.info("session_start send dropped on %s — client likely reconnecting", ws_id)
            return

        logger.info(
            "Device %s registered on session %s (resumed=%s, ws_id=%s)",
            device_id, session_id, resumed, ws_id,
        )

        # Audit C8/K15 (2026-04-20): on resume, replay the tail of the
        # message history so Tab5 chat can rehydrate its local store.
        # Previously session_start carried only message_count and Tab5
        # had to fetch via REST (which it never did) -- so a reconnect
        # lost the conversation from the user's view even though it was
        # on disk. Cap at 20 messages (most recent) to keep the WS frame
        # small; Tab5 can still fetch full history via
        # /api/v1/sessions/{id}/messages.
        if resumed and self._message_store is not None:
            try:
                msgs = await self._message_store.get_messages(
                    session_id, limit=20, offset=0
                )
                # Return the LAST 20 (get_messages returns ascending, so
                # slice the tail).
                tail = msgs[-20:] if len(msgs) > 20 else msgs
                items = []
                for m in tail:
                    role = m.get("role")
                    content = m.get("content")
                    if not role or not content:
                        continue
                    items.append({
                        "role": role,
                        "content": content,
                        "timestamp": m.get("created_at"),
                    })
                if items and not await self._safe_send_json(ws, {
                    "type": "session_messages",
                    "session_id": session_id,
                    "items": items,
                }):
                    logger.info("session_messages replay dropped on %s", ws_id)
                else:
                    logger.info("Replayed %d messages for session %s",
                                len(items), session_id)
            except Exception as e:
                logger.warning("session_messages replay failed: %s", e)

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
            backend_pool=self._backend_pool,
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

            # v4·D connectivity polish: emit a zero-cost receipt on the
            # TinkerClaw bypass path so the chat bubble gets stamped
            # ("claw-agent · FREE") instead of no stamp at all.  TC turns
            # don't expose token counts the way OpenRouter does; we just
            # surface the engine name so transparency-per-bubble still
            # holds.
            if not ws.closed:
                # Wave 8 audit #2 (A4/F3/J12): the first-turn fallback was
                # the bare string "tinkerclaw" which shows up in chat
                # bubbles as a generic stamp until the gateway populates
                # `_model`. Fall back to the LLMConfig default
                # ("minimax/MiniMax-M2.5") when both `name` and `_model`
                # are empty so the first bubble stamp is still honest.
                inner = getattr(llm, "_model", "") or ""
                conf_default = getattr(conn_cfg.llm, "tinkerclaw_model", "") or ""
                tc_model = (
                    inner
                    or getattr(llm, "name", None)
                    or conf_default
                    or "minimax/MiniMax-M2.5"
                )
                try:
                    await ws.send_json({
                        "type": "receipt",
                        "stage": "llm",
                        "model": tc_model,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                        "cost_mils": 0,          # TC bills to its own gateway
                        "llm_ms": 0,
                        "retried": False,
                        "retry_reason": "",
                    })
                except Exception:
                    logger.debug("TC receipt emit failed", exc_info=True)

            # Rich media detection for TinkerClaw responses too
            if full_response and self._media_pipeline:
                try:
                    media_events = await self._media_pipeline.process_response(
                        response_text, session_id
                    )
                    # Audit D6 (TC path): send text_update BEFORE media events
                    # so Tab5's last-bubble targeting still points at the
                    # text bubble when the clear arrives.
                    if media_events:
                        logger.info("Sent %d media events for TinkerClaw response", len(media_events))
                        cleaned = self._media_pipeline.strip_rendered_content(response_text, media_events)
                        logger.info("Text stripped: %d→%d chars", len(response_text), len(cleaned))
                        if not ws.closed:
                            await ws.send_json({"type": "text_update", "text": cleaned})
                            logger.info("Sent text_update (D6 TC) with %d chars", len(cleaned))
                    for event in media_events:
                        if not ws.closed:
                            await ws.send_json(event)
                except Exception as e:
                    logger.warning("TinkerClaw media detection failed: %s", e)

            return

        try:
            # Stream LLM response via conversation engine.
            #
            # Wave 10 audit #78 fix: buffer tokens client-side before
            # forwarding so a stray `<tool>...</tool><args>...</args>`
            # block emitted mid-stream by qwen3:1.7b (or any small model
            # with a shaky tool-call grammar) never reaches the chat
            # bubble. conversation.py already strips markup on the
            # final-yield fallthrough path, but the happy path yields
            # one token at a time — a raw `<tool>` tag can land in Tab5
            # before the LLM finishes emitting the closing `</tool>`.
            #
            # Strategy: hold tokens in a rolling buffer. When the buffer
            # contains a complete `<tool>...</args>` block, strip it
            # before flushing. Emit the remaining prefix on every tick
            # so streaming latency stays low for normal text.
            full_response = []
            pending = ""  # tokens not yet safe to forward
            import re as _re
            _TOOL_RE_LOCAL = _re.compile(
                r"<tool>[\s\S]*?</tool>\s*<args>[\s\S]*?</args>\s*>?",
                _re.IGNORECASE,
            )
            async for token in self._conversation.process_text_stream(
                session_id=session_id,
                text=content,
                input_mode="text",
                on_tool_call=conn_state.get("on_tool_call"),
                on_tool_result=conn_state.get("on_tool_result"),
            ):
                full_response.append(token)
                pending += token
                # Strip any complete tool blocks sitting in the pending
                # buffer. Substitute in-place so remaining prose still
                # flushes below.
                stripped = _TOOL_RE_LOCAL.sub("", pending)
                if stripped != pending:
                    pending = stripped
                # Hold back the tail if it looks like a partial tool
                # marker so we don't flush `<tool>dat` to the client and
                # then have to retract it.
                hold_at = -1
                for marker in ("<tool>", "<tool", "</tool", "<args", "</args"):
                    idx = pending.rfind(marker)
                    if idx >= 0 and idx > hold_at:
                        hold_at = idx
                if hold_at >= 0:
                    flush, pending = pending[:hold_at], pending[hold_at:]
                else:
                    flush, pending = pending, ""
                if flush and not ws.closed:
                    await ws.send_json({"type": "llm", "text": flush})
            # End-of-stream: flush whatever remains, stripped one more time.
            pending = _TOOL_RE_LOCAL.sub("", pending)
            if pending and not ws.closed:
                await ws.send_json({"type": "llm", "text": pending})

            response_text = _TOOL_RE_LOCAL.sub("", "".join(full_response))

            if not ws.closed:
                await ws.send_json({"type": "llm_done", "llm_ms": 0})

            # Rich media detection — scan response for image/chart/map references.
            # Audit D6: send text_update BEFORE media events. Tab5's
            # ui_chat_update_last_message targets the *last* chat bubble. If
            # we send media first, the image becomes "last" and the empty-
            # string text_update removes the wrong row. text_update first
            # clears the streamed markdown bubble; media events then append
            # the rendered JPEG below.
            if full_response:
                try:
                    media_events = await self._media_pipeline.process_response(
                        response_text, session_id
                    )
                    logger.info("MediaPipeline: %d event(s) for response len=%d",
                                len(media_events), len(response_text))
                    if media_events:
                        cleaned = self._media_pipeline.strip_rendered_content(
                            response_text, media_events
                        )
                        logger.info("strip_rendered_content: %d->%d chars",
                                    len(response_text), len(cleaned))
                        if not ws.closed:
                            await ws.send_json({"type": "text_update", "text": cleaned})
                            logger.info("Sent text_update (D6) with %d chars", len(cleaned))
                    for event in media_events:
                        if not ws.closed:
                            await ws.send_json(event)
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
                    # v4·D audit P1: mode-aware TTS synth budget.  Piper
                    # can take 15-25 s on Q6A ARM64 for a 200-word reply;
                    # cloud gpt-audio-mini is fast but still needs a
                    # cushion when OpenRouter edge adds latency.  The
                    # hardcoded 30 s was too tight in practice for local
                    # and wasteful for cloud.
                    tts_backend = (conn_cfg.tts.backend if conn_cfg else "piper")
                    tts_timeout = 90 if tts_backend != "openrouter" else 30
                    audio_bytes = await asyncio.wait_for(
                        pipeline._tts.synthesize(response_text),
                        timeout=tts_timeout,
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
                        # Audit F5 (2026-04-20): TTS receipt for text-path
                        # synthesis so chat bubbles surface the TTS backend
                        # that spoke the reply.
                        try:
                            await ws.send_json({
                                "type": "receipt",
                                "stage": "tts",
                                "model": tts_backend,
                                "tts_ms": round(tts_ms),
                                "cost_mils": 0,
                            })
                        except Exception:
                            pass
                except Exception:
                    logger.exception("TTS for text input failed")
                    # Always send tts_end so Tab5 doesn't hang in SPEAKING
                    if not ws.closed:
                        await ws.send_json({"type": "tts_end", "tts_ms": 0})

            logger.info("Text response on session %s: %s", session_id, response_text[:80])

            # Phase 3 per-turn receipt for text-path turns. Voice-path
            # receipts are emitted from pipeline._process_utterance; the
            # text path reaches the LLM via ConversationEngine directly
            # and bypasses pipeline entirely, so we emit here too.
            try:
                convo = conn_state.get("conversation") or self._conversation
                cur_llm = getattr(convo, "_llm", None)
                if cur_llm is not None and hasattr(cur_llm, "get_last_usage"):
                    usage = cur_llm.get_last_usage()
                    if usage and usage.get("total_tokens"):
                        from dragon_voice.llm.openrouter_llm import price_for_model
                        cost_mils = price_for_model(
                            usage["model"],
                            usage.get("prompt_tokens", 0),
                            usage.get("completion_tokens", 0),
                        )
                        if not ws.closed:
                            await ws.send_json({
                                "type":              "receipt",
                                "stage":             "llm",
                                "model":             usage["model"],
                                "prompt_tokens":     usage.get("prompt_tokens", 0),
                                "completion_tokens": usage.get("completion_tokens", 0),
                                "total_tokens":      usage.get("total_tokens", 0),
                                "cost_mils":         cost_mils,
                                # v4·D Gauntlet G2 surface retries
                                "retried":           bool(usage.get("retried", False)),
                                "retry_reason":      usage.get("retry_reason", ""),
                            })
                        logger.info(
                            "Receipt emitted (text): model=%s tok=%d+%d=%d cost_mils=%d",
                            usage["model"],
                            usage.get("prompt_tokens", 0),
                            usage.get("completion_tokens", 0),
                            usage.get("total_tokens", 0),
                            cost_mils,
                        )
            except Exception:
                logger.exception("Text-path receipt emit failed")

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

        # Wave 13 H2: image may be up to ~8 MB (camera JPEG) -- reading on the
        # event loop thread stalls every other WS connection. Offload the
        # blocking read + base64 to the default executor.
        def _read_and_encode(path: str) -> str:
            with open(path, "rb") as f:
                return base64.b64encode(f.read()).decode()
        image_b64 = await asyncio.to_thread(_read_and_encode, image_path)

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

        # Wave 14 W14-C06: cancel any per-connection background tasks
        # (e.g. cap_downgrade speak_system) before we shut down the pipeline
        # they depend on. Without this, the orphan task holds the Piper
        # subprocess + TTS lock past WS close.
        bg_tasks = conn_state.get("bg_tasks") or set()
        if bg_tasks:
            for t in list(bg_tasks):
                t.cancel()
            await asyncio.gather(*bg_tasks, return_exceptions=True)

        # v4·D Phase 4g: unregister the session's surface so skills that
        # kept a reference to it start seeing dropped sends explicitly.
        if session_id and self._surface_mgr is not None:
            try:
                await self._surface_mgr.unregister_session(session_id)
            except Exception:
                logger.debug("surface unregister failed")

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
