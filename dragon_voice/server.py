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

from dragon_voice.api import APIRoutes
from dragon_voice.config import VoiceConfig, config_to_dict, load_config
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
        app = web.Application(client_max_size=32 * 1024 * 1024)  # 32MB for audio uploads

        # HTTP routes (legacy)
        app.router.add_get("/", self._handle_status)
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/api/config", self._handle_get_config)
        app.router.add_post("/api/config", self._handle_set_config)

        # WebSocket route
        app.router.add_get("/ws/voice", self._handle_ws_voice)

        # Lifecycle hooks
        app.on_startup.append(self._on_startup)
        app.on_shutdown.append(self._on_shutdown)

        self._app = app
        return app

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

        # Conversation engine (shared LLM backend for text/API input)
        self._conversation = ConversationEngine(
            self._db, self._message_store, self._config.llm
        )
        await self._conversation.initialize()

        # REST API routes
        api = APIRoutes(self._db, self._session_mgr, self._message_store,
                        self._conversation, voice_config=self._config)
        api.register(app)

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
        except Exception as e:
            logger.warning("Notes API not available: %s", e)

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
                                await pipeline.finish_dictation()
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
                        # Tab5 requests cloud mode toggle
                        cloud_mode = cmd.get("cloud_mode")
                        if cloud_mode is not None:
                            stt_be = "openrouter" if cloud_mode else "moonshine"
                            tts_be = "openrouter" if cloud_mode else "piper"
                            logger.info("Connection %s: cloud_mode=%s → stt=%s tts=%s",
                                        ws_id, cloud_mode, stt_be, tts_be)
                            # Update config and hot-swap backends
                            self._config.stt.backend = stt_be
                            self._config.tts.backend = tts_be
                            # Propagate API key for cloud backends
                            if cloud_mode:
                                self._config.stt.openrouter_api_key = self._config.llm.openrouter_api_key
                                self._config.stt.openrouter_url = self._config.llm.openrouter_url
                                self._config.tts.openrouter_api_key = self._config.llm.openrouter_api_key
                                self._config.tts.openrouter_url = self._config.llm.openrouter_url
                            # Swap backends on active pipeline
                            pipeline = conn_state.get("pipeline")
                            if pipeline:
                                await pipeline.swap_backends(self._config)
                            # Confirm to Tab5
                            if not ws.closed:
                                await ws.send_json({
                                    "type": "config_update",
                                    "config": {
                                        "stt": stt_be, "tts": tts_be,
                                        "llm": self._config.llm.backend,
                                        "cloud_mode": bool(cloud_mode),
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

        # Create voice pipeline with conversation engine for multi-turn
        pipeline = VoicePipeline(
            self._config, on_audio, on_event,
            conversation_engine=self._conversation,
            session_id=session_id,
        )
        try:
            await pipeline.initialize()
        except Exception as e:
            logger.exception("Failed to initialize pipeline for %s", ws_id)
            await ws.send_json({"type": "error", "code": "internal",
                                "message": f"Pipeline init failed: {e}"})
            return

        # Update connection state
        conn_state["pipeline"] = pipeline
        conn_state["session_id"] = session_id
        conn_state["device_id"] = device_id
        conn_state["registered"] = True
        conn_state["response_mode"] = "always_speak"  # voice device gets TTS

        # Send session_start response (per protocol.md)
        await ws.send_json({
            "type": "session_start",
            "session_id": session_id,
            "device_id": device_id,
            "resumed": resumed,
            "message_count": session.get("message_count", 0),
            "config": {
                "stt": pipeline.stt_name,
                "tts": pipeline.tts_name,
                "llm": pipeline.llm_name,
                "tts_sample_rate": pipeline.tts_sample_rate,
                "response_mode": "match_input",
                "system_prompt": self._config.llm.system_prompt,
            },
        })

        logger.info(
            "Device %s registered on session %s (resumed=%s, ws_id=%s)",
            device_id, session_id, resumed, ws_id,
        )

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
        pipeline = conn_state.get("pipeline")

        # Pause session (not end — it can be resumed)
        if session_id and self._session_mgr:
            await self._session_mgr.pause_session(session_id)

        # Mark device offline
        if device_id and self._db:
            await self._db.set_device_online(device_id, False)
            await self._db.add_event(
                "device.disconnected", device_id=device_id,
                data={"session_id": session_id},
            )

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
