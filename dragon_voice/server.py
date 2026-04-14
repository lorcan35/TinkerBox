"""WebSocket server for Dragon Voice.

Serves the voice pipeline over WebSocket and provides HTTP endpoints
for health checks, status, configuration, and the REST API.

Integrates: Database, SessionManager, MessageStore, ConversationEngine, API routes.

refs #16, #17, #18
"""

import asyncio
import json
import logging
import time
from typing import Optional

from aiohttp import web, WSMsgType

from dragon_voice.api import setup_all_routes
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

        # Backend names for status page
        self._stt_name = config.stt.backend
        self._tts_name = config.tts.backend
        self._llm_name = config.llm.backend

        # Foundation modules (initialized in on_startup)
        self._db: Optional[Database] = None
        self._session_mgr: Optional[SessionManager] = None
        self._message_store: Optional[MessageStore] = None
        self._conversation: Optional[ConversationEngine] = None
        self._notes_svc = None

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

    @web.middleware
    async def _cors_middleware(self, request: web.Request, handler):
        """Add CORS headers to all API responses."""
        # Handle preflight OPTIONS requests
        if request.method == "OPTIONS":
            return web.Response(headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, X-Sample-Rate, Accept",
                "Access-Control-Max-Age": "3600",
            })
        response = await handler(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
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
            import aiohttp as _aiohttp
            timeout = _aiohttp.ClientTimeout(total=30)
            async with _aiohttp.ClientSession(timeout=timeout) as session:
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

        logger.info("Foundation modules initialized")

    async def _on_shutdown(self, app: web.Application) -> None:
        """Clean up all active sessions and foundation modules on server shutdown."""
        logger.info("Server shutting down — closing %d connections", len(self._active_connections))

        # Shut down pipelines
        tasks = []
        for ws_id, conn in list(self._active_connections.items()):
            pipeline = conn.get("pipeline")
            if pipeline:
                tasks.append(pipeline.shutdown())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active_connections.clear()

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

            # Swap backends on all active pipelines
            swap_tasks = []
            for ws_id, conn in self._active_connections.items():
                pipeline = conn.get("pipeline")
                if pipeline:
                    logger.info("Swapping backends for connection %s", ws_id)
                    swap_tasks.append(pipeline.swap_backends(new_config))

            if swap_tasks:
                await asyncio.gather(*swap_tasks, return_exceptions=True)

            return web.json_response(
                {
                    "status": "ok",
                    "message": f"Config updated, {len(swap_tasks)} pipelines reloaded",
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
            heartbeat=600.0,
        )
        await ws.prepare(request)

        ws_id = f"ws{self._session_count}"
        self._session_count += 1
        peer = request.remote or "unknown"
        logger.info("WebSocket connected: %s (ws_id=%s)", peer, ws_id)

        # Connection state — populated after register
        conn_state: dict = {
            "ws_id": ws_id,
            "pipeline": None,
            "session_id": None,
            "device_id": None,
            "registered": False,
            "mode": "ask",  # "ask" or "dictate"
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
                except Exception:
                    pass

        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    # Raw PCM audio data — forward to pipeline
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
                        await self._handle_register(ws, conn_state, cmd, on_audio, on_event)

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
                            await pipeline.process_segment()

                    elif cmd_type == "stop":
                        pipeline = conn_state.get("pipeline")
                        if pipeline:
                            mode = conn_state.get("mode", "ask")
                            buf_size = len(pipeline._audio_buffer) + len(pipeline._segment_buffer)
                            logger.info("Connection %s: stop (mode=%s, buffer=%d bytes)", ws_id, mode, buf_size)
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
                                device_id=device_id, type="conversation"
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
                        await self._handle_text(ws, conn_state, cmd)

                    elif cmd_type == "record_start" or cmd_type == "record_stop":
                        # Superseded by dictation mode (start with mode=dictate)
                        logger.info("Connection %s: %s (use mode=dictate instead)", ws_id, cmd_type)

                    elif cmd_type == "ping":
                        # ESP-IDF sends application-level pings (LEARNINGS.md #11)
                        await ws.send_json({"type": "pong"})

                    elif cmd_type == "config_update":
                        # Three-tier voice mode: 0=local, 1=hybrid, 2=cloud
                        voice_mode = cmd.get("voice_mode")
                        llm_model = cmd.get("llm_model")
                        # Backward compat: old binary cloud_mode toggle
                        cloud_mode = cmd.get("cloud_mode")
                        if cloud_mode is not None and voice_mode is None:
                            voice_mode = 2 if cloud_mode else 0

                        if voice_mode is not None:
                            # STT+TTS: local for mode 0, cloud for mode 1+2
                            if voice_mode == 0:
                                stt_be, tts_be = "moonshine", "piper"
                            else:
                                stt_be, tts_be = "openrouter", "openrouter"

                            # LLM: cloud only for mode 2, local for 0+1
                            if voice_mode == 2:
                                llm_be = "openrouter"
                                if llm_model:
                                    self._config.llm.openrouter_model = llm_model
                            else:
                                llm_be = self._config.llm.local_backend or "ollama"
                                # Local model picker: user can select qwen3:0.6b/1.7b/4b etc.
                                # Only apply if it looks like an Ollama model (no '/' = not a cloud model ID)
                                if llm_model and llm_be == "ollama" and "/" not in llm_model:
                                    self._config.llm.ollama_model = llm_model
                                    logger.info("Local model switched to: %s", llm_model)

                            # Apply mode-aware system prompt and max_tokens
                            if voice_mode == 0:
                                self._config.llm.system_prompt = SYSTEM_PROMPT_LOCAL
                                self._config.llm.max_tokens = MAX_TOKENS_LOCAL
                            elif voice_mode == 1:
                                self._config.llm.system_prompt = SYSTEM_PROMPT_HYBRID
                                self._config.llm.max_tokens = MAX_TOKENS_HYBRID
                            else:
                                self._config.llm.system_prompt = SYSTEM_PROMPT_CLOUD
                                self._config.llm.max_tokens = MAX_TOKENS_CLOUD

                            logger.info("Connection %s: voice_mode=%d → stt=%s tts=%s llm=%s model=%s tokens=%d",
                                        ws_id, voice_mode, stt_be, tts_be, llm_be,
                                        self._config.llm.openrouter_model if voice_mode == 2 else "(local)",
                                        self._config.llm.max_tokens)

                            # Validate API key for cloud modes
                            if voice_mode >= 1 and not self._config.llm.openrouter_api_key:
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
                                        sid, system_prompt=self._config.llm.system_prompt
                                    )
                                except Exception:
                                    logger.warning("Failed to update session system_prompt")

                            # Apply config
                            self._config.stt.backend = stt_be
                            self._config.tts.backend = tts_be
                            self._config.llm.backend = llm_be

                            # Propagate API keys for cloud backends
                            if voice_mode >= 1:
                                self._config.stt.openrouter_api_key = self._config.llm.openrouter_api_key
                                self._config.stt.openrouter_url = self._config.llm.openrouter_url
                                self._config.tts.openrouter_api_key = self._config.llm.openrouter_api_key
                                self._config.tts.openrouter_url = self._config.llm.openrouter_url

                            # Hot-swap backends on pipeline AND conversation engine
                            pipeline = conn_state.get("pipeline")
                            if pipeline:
                                try:
                                    await pipeline.swap_backends(self._config)
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
                                    new_llm = create_llm(self._config.llm)
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
                                    active_model = self._config.llm.openrouter_model
                                elif llm_be == "ollama":
                                    active_model = self._config.llm.ollama_model
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
            system_prompt=self._config.llm.system_prompt,
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
                    "stt": self._config.stt.backend,
                    "tts": self._config.tts.backend,
                    "llm": self._config.llm.backend,
                    "tts_sample_rate": self._config.audio.input_sample_rate,
                    "response_mode": "match_input",
                    "system_prompt": self._config.llm.system_prompt,
                },
            })
        except Exception as e:
            logger.warning("Failed to send session_start to %s: %s (client may have disconnected)", ws_id, e)
            return

        logger.info(
            "Device %s registered on session %s (resumed=%s, ws_id=%s)",
            device_id, session_id, resumed, ws_id,
        )

        # Reset config to local defaults before pipeline init.
        # Tab5 will immediately send config_update with its actual mode,
        # so this avoids initializing cloud backends only to swap them out.
        self._config.stt.backend = "moonshine"
        self._config.tts.backend = "piper"
        self._config.llm.backend = self._config.llm.local_backend or "ollama"
        self._config.llm.system_prompt = SYSTEM_PROMPT_LOCAL
        self._config.llm.max_tokens = MAX_TOKENS_LOCAL

        # NOW initialize the voice pipeline (slow: Moonshine load ~2s)
        # This happens AFTER session_start is sent so Tab5 doesn't timeout
        pipeline = VoicePipeline(
            self._config, on_audio, on_event,
            conversation_engine=self._conversation,
            session_id=session_id,
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

        logger.info("Text input on session %s: %s", session_id, content[:80])

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
                        target_rate = self._config.audio.input_sample_rate
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

    async def _handle_disconnect(self, conn_state: dict) -> None:
        """Handle WebSocket disconnect: pause session, mark device offline."""
        session_id = conn_state.get("session_id")
        device_id = conn_state.get("device_id")
        ws_id = conn_state.get("ws_id")
        pipeline = conn_state.get("pipeline")

        # Pause session (not end — it can be resumed)
        if session_id and self._session_mgr:
            await self._session_mgr.pause_session(session_id)

        # Mark device offline ONLY if no other active connection for same device.
        # Prevents race: old connection disconnect runs after new boot's register,
        # which would incorrectly mark the device offline.
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
            else:
                logger.info("Device %s still has active connection — keeping online", device_id)

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
