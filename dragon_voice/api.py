"""REST API routes for TinkerClaw (v1).

Provides HTTP endpoints for sessions, messages, devices, config, and transcription.
Registered on the aiohttp app by the server module.

refs #21
"""

import json
import logging
import time
from typing import Any

from aiohttp import web

from dragon_voice.db import Database
from dragon_voice.sessions import SessionManager
from dragon_voice.messages import MessageStore
from dragon_voice.conversation import ConversationEngine
from dragon_voice.config import VoiceConfig
from dragon_voice.stt import create_stt, STTBackend

logger = logging.getLogger(__name__)


def _json_error(message: str, status: int = 400) -> web.Response:
    """Return a JSON error response."""
    return web.json_response({"error": message}, status=status)


def _paginated_response(items: list[dict], limit: int, offset: int) -> web.Response:
    """Return a paginated JSON response."""
    return web.json_response({
        "items": items,
        "count": len(items),
        "limit": limit,
        "offset": offset,
    })


class APIRoutes:
    """REST API route handlers for TinkerClaw v1.

    All routes are prefixed with /api/v1/.
    """

    def __init__(
        self,
        db: Database,
        session_mgr: SessionManager,
        message_store: MessageStore,
        conversation: ConversationEngine | None = None,
        voice_config: VoiceConfig | None = None,
    ) -> None:
        self._db = db
        self._session_mgr = session_mgr
        self._messages = message_store
        self._conversation = conversation
        self._voice_config = voice_config
        self._stt: STTBackend | None = None  # lazy-initialized on first transcribe

    def register(self, app: web.Application) -> None:
        """Register all API routes on the aiohttp app."""
        # Sessions
        app.router.add_get("/api/v1/sessions", self.list_sessions)
        app.router.add_post("/api/v1/sessions", self.create_session)
        app.router.add_get("/api/v1/sessions/{session_id}", self.get_session)
        app.router.add_post("/api/v1/sessions/{session_id}/end", self.end_session)

        # Messages
        app.router.add_get("/api/v1/sessions/{session_id}/messages", self.list_messages)
        app.router.add_post("/api/v1/sessions/{session_id}/chat", self.send_chat)

        # Devices
        app.router.add_get("/api/v1/devices", self.list_devices)
        app.router.add_get("/api/v1/devices/{device_id}", self.get_device)

        # Config
        app.router.add_get("/api/v1/config", self.list_config)
        app.router.add_get("/api/v1/config/{key}", self.get_config)
        app.router.add_put("/api/v1/config/{key}", self.set_config)

        # Events
        app.router.add_get("/api/v1/events", self.list_events)

        # Transcription
        app.router.add_post("/api/v1/transcribe", self.transcribe_audio)

        # OTA firmware updates
        app.router.add_get("/api/ota/check", self.ota_check)
        app.router.add_get("/api/ota/firmware.bin", self.ota_firmware)

        logger.info("API v1 routes registered (incl. OTA)")

    # ── Sessions ───────────────────────────────────────────────────────

    async def list_sessions(self, request: web.Request) -> web.Response:
        """GET /api/v1/sessions?device_id=&status=&limit=&offset="""
        device_id = request.query.get("device_id")
        status = request.query.get("status")
        limit = min(int(request.query.get("limit", "50")), 200)
        offset = int(request.query.get("offset", "0"))

        sessions = await self._session_mgr.list_sessions(
            device_id=device_id, status=status, limit=limit, offset=offset
        )
        return _paginated_response(sessions, limit, offset)

    async def create_session(self, request: web.Request) -> web.Response:
        """POST /api/v1/sessions {device_id?, type?, system_prompt?}"""
        try:
            body = await request.json()
        except Exception:
            return _json_error("Invalid JSON body")

        session = await self._session_mgr.create_session(
            device_id=body.get("device_id"),
            session_type=body.get("type", "conversation"),
            system_prompt=body.get("system_prompt", ""),
            config=body.get("config"),
        )
        return web.json_response(session, status=201)

    async def get_session(self, request: web.Request) -> web.Response:
        """GET /api/v1/sessions/{session_id}"""
        session_id = request.match_info["session_id"]
        session = await self._session_mgr.get_session(session_id)
        if not session:
            return _json_error("Session not found", 404)
        return web.json_response(session)

    async def end_session(self, request: web.Request) -> web.Response:
        """POST /api/v1/sessions/{session_id}/end"""
        session_id = request.match_info["session_id"]
        session = await self._session_mgr.get_session(session_id)
        if not session:
            return _json_error("Session not found", 404)

        await self._session_mgr.end_session(session_id)
        return web.json_response({"status": "ended", "session_id": session_id})

    # ── Messages ───────────────────────────────────────────────────────

    async def list_messages(self, request: web.Request) -> web.Response:
        """GET /api/v1/sessions/{session_id}/messages?limit=&offset="""
        session_id = request.match_info["session_id"]

        # Verify session exists
        session = await self._session_mgr.get_session(session_id)
        if not session:
            return _json_error("Session not found", 404)

        limit = min(int(request.query.get("limit", "100")), 500)
        offset = int(request.query.get("offset", "0"))

        messages = await self._messages.get_messages(
            session_id, limit=limit, offset=offset
        )
        return _paginated_response(messages, limit, offset)

    async def send_chat(self, request: web.Request) -> web.Response:
        """POST /api/v1/sessions/{session_id}/chat {text}

        Send a text message and stream back the LLM response as SSE.
        """
        session_id = request.match_info["session_id"]
        session = await self._session_mgr.get_session(session_id)
        if not session:
            return _json_error("Session not found", 404)

        if not self._conversation:
            return _json_error("Conversation engine not available", 503)

        try:
            body = await request.json()
        except Exception:
            return _json_error("Invalid JSON body")

        text = body.get("text", "").strip()
        if not text:
            return _json_error("'text' field is required")

        # Stream SSE response
        response = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Access-Control-Allow-Origin": "*",
        })
        await response.prepare(request)

        try:
            async for token in self._conversation.process_text_stream(
                session_id=session_id,
                text=text,
                input_mode="text",
            ):
                data = json.dumps({"token": token})
                await response.write(f"data: {data}\n\n".encode())
        except Exception as e:
            logger.exception("Chat error on session %s", session_id)
            await response.write(f"data: {json.dumps({'error': str(e)})}\n\n".encode())

        await response.write(b"data: [DONE]\n\n")
        return response

    # ── Devices ────────────────────────────────────────────────────────

    async def list_devices(self, request: web.Request) -> web.Response:
        """GET /api/v1/devices?online=true"""
        online_only = request.query.get("online", "").lower() in ("true", "1")
        devices = await self._db.list_devices(online_only=online_only)
        return web.json_response({"items": devices, "count": len(devices)})

    async def get_device(self, request: web.Request) -> web.Response:
        """GET /api/v1/devices/{device_id}"""
        device_id = request.match_info["device_id"]
        device = await self._db.get_device(device_id)
        if not device:
            return _json_error("Device not found", 404)
        return web.json_response(device)

    # ── Config Store ───────────────────────────────────────────────────

    async def list_config(self, request: web.Request) -> web.Response:
        """GET /api/v1/config?scope=global&scope_id="""
        scope = request.query.get("scope", "global")
        scope_id = request.query.get("scope_id")
        entries = await self._db.list_config(scope, scope_id)
        return web.json_response({"scope": scope, "scope_id": scope_id, "entries": entries})

    async def get_config(self, request: web.Request) -> web.Response:
        """GET /api/v1/config/{key}?scope=global&scope_id=&resolve=false

        If resolve=true, applies scope resolution: session > device > global.
        """
        key = request.match_info["key"]
        resolve = request.query.get("resolve", "").lower() in ("true", "1")

        if resolve:
            device_id = request.query.get("device_id")
            session_id = request.query.get("session_id")
            value = await self._db.get_resolved_config(key, device_id, session_id)
        else:
            scope = request.query.get("scope", "global")
            scope_id = request.query.get("scope_id")
            value = await self._db.get_config(key, scope, scope_id)

        if value is None:
            return _json_error(f"Config key '{key}' not found", 404)

        return web.json_response({"key": key, "value": json.loads(value)})

    async def set_config(self, request: web.Request) -> web.Response:
        """PUT /api/v1/config/{key} {value, scope?, scope_id?}"""
        key = request.match_info["key"]
        try:
            body = await request.json()
        except Exception:
            return _json_error("Invalid JSON body")

        if "value" not in body:
            return _json_error("'value' field is required")

        scope = body.get("scope", "global")
        scope_id = body.get("scope_id")
        value_json = json.dumps(body["value"])

        await self._db.set_config(key, value_json, scope, scope_id)
        return web.json_response({"key": key, "value": body["value"], "scope": scope})

    # ── Events ────────────────────────────────────────────────────────

    async def list_events(self, request: web.Request) -> web.Response:
        """GET /api/v1/events?since_id=0&type=&session_id=&limit=50"""
        since_id = int(request.query.get("since_id", "0"))
        event_type = request.query.get("type")
        session_id = request.query.get("session_id")
        limit = min(int(request.query.get("limit", "50")), 200)

        events = await self._db.get_events(
            event_type=event_type,
            session_id=session_id,
            since_id=since_id,
            limit=limit,
        )
        return web.json_response({
            "items": events,
            "count": len(events),
        })

    # ── Transcription ─────────────────────────────────────────────────

    async def _ensure_stt(self) -> STTBackend | None:
        """Lazy-initialize STT backend for transcription API."""
        if self._stt:
            return self._stt
        if not self._voice_config:
            return None
        self._stt = create_stt(self._voice_config.stt)
        await self._stt.initialize()
        logger.info("STT backend initialized for transcription API: %s", self._stt.name)
        return self._stt

    async def transcribe_audio(self, request: web.Request) -> web.Response:
        """POST /api/v1/transcribe

        Accepts raw PCM int16 mono audio (16kHz) or WAV file in the request body.
        Returns the transcript.

        Headers:
            Content-Type: application/octet-stream (raw PCM) or audio/wav
            X-Sample-Rate: 16000 (optional, default 16000)

        Body: raw audio bytes

        Response: {"text": "transcribed text", "duration_s": 5.2, "stt_ms": 1234}
        """
        stt = await self._ensure_stt()
        if not stt:
            return _json_error("STT backend not available", 503)

        content_type = request.content_type or "application/octet-stream"
        sample_rate = int(request.headers.get("X-Sample-Rate", "16000"))

        # Read audio data
        audio_bytes = await request.read()
        if not audio_bytes or len(audio_bytes) < 100:
            return _json_error("No audio data in request body")

        # If WAV, strip the 44-byte header to get raw PCM
        if content_type == "audio/wav" or (len(audio_bytes) > 4 and audio_bytes[:4] == b"RIFF"):
            # Find "data" chunk
            data_pos = audio_bytes.find(b"data")
            if data_pos >= 0 and data_pos + 8 <= len(audio_bytes):
                audio_bytes = audio_bytes[data_pos + 8:]  # skip "data" + 4-byte size
            else:
                audio_bytes = audio_bytes[44:]  # fallback: assume standard 44-byte header

        duration_s = len(audio_bytes) / (sample_rate * 2)  # int16 = 2 bytes/sample
        logger.info("Transcribe request: %.1fs audio (%d bytes, %dHz)",
                    duration_s, len(audio_bytes), sample_rate)

        try:
            t0 = time.monotonic()
            transcript = await stt.transcribe(audio_bytes, sample_rate)
            stt_ms = (time.monotonic() - t0) * 1000

            logger.info("Transcribed (%.0fms): %s", stt_ms, transcript[:80])
            return web.json_response({
                "text": transcript.strip(),
                "duration_s": round(duration_s, 1),
                "stt_ms": round(stt_ms),
            })
        except Exception as e:
            logger.exception("Transcription failed")
            return _json_error(f"Transcription failed: {e}", 500)

    # ------------------------------------------------------------------ OTA

    # OTA firmware directory: /home/radxa/ota/ (create manually, place .bin files here)
    OTA_DIR = "/home/radxa/ota"
    OTA_VERSION_FILE = "/home/radxa/ota/version.json"

    async def ota_check(self, request: web.Request) -> web.Response:
        """Check if firmware update is available.

        Query params: current=<version>
        Returns: {"update": true/false, "version": "...", "url": "...", "sha256": "..."}

        Place firmware in /home/radxa/ota/tinkertab.bin and create
        /home/radxa/ota/version.json with {"version": "0.6.1", "sha256": "..."}.
        """
        import os
        current = request.query.get("current", "0.0.0")
        logger.info("OTA check from device (current: %s)", current)

        if not os.path.exists(self.OTA_VERSION_FILE):
            return web.json_response({"update": False, "current": current})

        try:
            with open(self.OTA_VERSION_FILE) as f:
                info = json.load(f)
        except Exception:
            return web.json_response({"update": False, "current": current})

        available_ver = info.get("version", "0.0.0")
        sha256 = info.get("sha256", "")

        # Simple version compare (works for semver like "0.6.0" < "0.6.1")
        if available_ver <= current:
            logger.info("No update: device=%s, available=%s", current, available_ver)
            return web.json_response({"update": False, "current": current,
                                       "available": available_ver})

        # Build firmware URL — use request host so it works on LAN and ngrok
        host = request.host  # e.g. "192.168.1.89:3502"
        scheme = request.scheme  # "http" or "https"
        firmware_url = f"{scheme}://{host}/api/ota/firmware.bin"

        logger.info("Update available: %s → %s (url: %s)", current, available_ver, firmware_url)
        return web.json_response({
            "update": True,
            "version": available_ver,
            "url": firmware_url,
            "sha256": sha256,
        })

    async def ota_firmware(self, request: web.Request) -> web.StreamResponse:
        """Serve the firmware binary file for OTA download."""
        import os
        firmware_path = os.path.join(self.OTA_DIR, "tinkertab.bin")

        if not os.path.exists(firmware_path):
            return web.Response(text="No firmware available", status=404)

        file_size = os.path.getsize(firmware_path)
        logger.info("Serving OTA firmware: %s (%d bytes)", firmware_path, file_size)

        resp = web.StreamResponse()
        resp.content_type = "application/octet-stream"
        resp.content_length = file_size
        resp.headers["Content-Disposition"] = "attachment; filename=tinkertab.bin"
        await resp.prepare(request)

        with open(firmware_path, "rb") as f:
            while chunk := f.read(8192):
                await resp.write(chunk)

        return resp
