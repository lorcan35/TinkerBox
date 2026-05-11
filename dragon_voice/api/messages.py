"""Message listing, chat SSE, and management API routes."""

import json
import logging

from aiohttp import web

from dragon_voice.api.utils import json_error, paginated_response, parse_pagination
from dragon_voice.db import Database
from dragon_voice.sessions import SessionManager
from dragon_voice.messages import MessageStore
from dragon_voice.conversation import ConversationEngine

logger = logging.getLogger(__name__)


class MessageRoutes:
    def __init__(self, db: Database, session_mgr: SessionManager,
                 message_store: MessageStore, conversation: ConversationEngine | None = None) -> None:
        self._db = db
        self._session_mgr = session_mgr
        self._messages = message_store
        self._conversation = conversation

    def register(self, app: web.Application) -> None:
        app.router.add_get("/api/v1/sessions/{session_id}/messages", self.list_messages)
        app.router.add_post("/api/v1/sessions/{session_id}/chat", self.send_chat)
        # Sprint 1: new endpoints
        app.router.add_get("/api/v1/messages/{message_id}", self.get_message)
        app.router.add_delete("/api/v1/sessions/{session_id}/messages", self.delete_messages)
        # Wave 3-C-a (cross-stack cohesion audit 2026-05-11): Tab5
        # POSTs SOLO/K144 turn pairs here so Dragon becomes the
        # canonical chat-message store across all 6 voice modes.
        app.router.add_post("/api/v1/sessions/{session_id}/messages", self.add_message)

    async def list_messages(self, request: web.Request) -> web.Response:
        """GET /api/v1/sessions/{session_id}/messages"""
        session_id = request.match_info["session_id"]
        session = await self._session_mgr.get_session(session_id)
        if not session:
            return json_error("Session not found", 404)
        limit, offset = parse_pagination(request, default_limit=100, max_limit=500)
        messages = await self._messages.get_messages(session_id, limit=limit, offset=offset)
        return paginated_response(messages, limit, offset)

    async def send_chat(self, request: web.Request) -> web.Response:
        """POST /api/v1/sessions/{session_id}/chat — SSE streaming LLM response"""
        session_id = request.match_info["session_id"]
        session = await self._session_mgr.get_session(session_id)
        if not session:
            return json_error("Session not found", 404)
        if not self._conversation:
            return json_error("Conversation engine not available", 503)

        try:
            body = await request.json()
        except Exception:
            return json_error("Invalid JSON body")

        text = body.get("text", "").strip()
        if not text:
            return json_error("'text' field is required")

        # Wave 14 W14-H05: drop the hardcoded Access-Control-Allow-Origin: *
        # — it silently bypassed the 4-origin allowlist enforced by
        # VoiceServer._cors_middleware.  The middleware stamps the correct
        # origin on ALLOWED cross-origin requests; non-browser callers don't
        # need the header at all.
        response = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
        })
        await response.prepare(request)

        try:
            async for token in self._conversation.process_text_stream(
                session_id=session_id, text=text, input_mode="text",
            ):
                data = json.dumps({"token": token})
                await response.write(f"data: {data}\n\n".encode())
        except Exception as e:
            logger.exception("Chat error on session %s", session_id)
            await response.write(f"data: {json.dumps({'error': str(e)})}\n\n".encode())

        await response.write(b"data: [DONE]\n\n")
        return response

    # ── Sprint 1: New endpoints ──

    async def get_message(self, request: web.Request) -> web.Response:
        """GET /api/v1/messages/{message_id}"""
        message_id = request.match_info["message_id"]
        msg = await self._db.get_message(message_id)
        if not msg:
            return json_error("Message not found", 404)
        return web.json_response(msg)

    async def delete_messages(self, request: web.Request) -> web.Response:
        """DELETE /api/v1/sessions/{session_id}/messages — purge all messages"""
        session_id = request.match_info["session_id"]
        session = await self._session_mgr.get_session(session_id)
        if not session:
            return json_error("Session not found", 404)
        deleted = await self._db.delete_messages(session_id)
        return web.json_response({"status": "purged", "session_id": session_id, "deleted_count": deleted})

    # ── Wave 3-C-a ──

    _ALLOWED_ROLES = ("user", "assistant", "system", "tool")
    _ALLOWED_INPUT_MODES = ("text", "voice", "system")

    async def add_message(self, request: web.Request) -> web.Response:
        """POST /api/v1/sessions/{session_id}/messages — append a turn.

        Wave 3-C-a (cross-stack cohesion audit 2026-05-11).  Tab5 POSTs
        SOLO and ONBOARD turn pairs here so the messages DB is the
        canonical chat log across all 6 voice modes.

        Body (JSON):
            role      : "user" | "assistant" | "system" | "tool"  (required)
            content   : str                                       (required)
            input_mode: "text" | "voice" | "system"               (default "text")
            model     : str | null                                (optional)
            token_count    : int | null                           (optional)
            latency_ms     : float | null                         (optional)
            audio_duration_s : float | null                       (optional)
            interrupted    : bool                                 (default false)
            media_id  : str | null                                (optional, multimodal)

        Returns 201 + the created message row.  404 if session not
        found.  400 on validation failure.
        """
        session_id = request.match_info["session_id"]
        session = await self._session_mgr.get_session(session_id)
        if not session:
            return json_error("Session not found", 404)

        try:
            body = await request.json()
        except Exception:
            return json_error("Invalid JSON body")
        if not isinstance(body, dict):
            return json_error("Body must be a JSON object")

        role = body.get("role")
        if role not in self._ALLOWED_ROLES:
            return json_error(
                f"'role' must be one of {self._ALLOWED_ROLES}; got {role!r}"
            )

        content = body.get("content")
        if not isinstance(content, str) or content == "":
            # Allow empty-content turns through MessageStore directly
            # only via the WS path (assistant placeholder bubbles); REST
            # callers always have real content because they're posting
            # after the turn completes.
            return json_error("'content' must be a non-empty string")

        input_mode = body.get("input_mode", "text")
        if input_mode not in self._ALLOWED_INPUT_MODES:
            return json_error(
                f"'input_mode' must be one of {self._ALLOWED_INPUT_MODES}; got {input_mode!r}"
            )

        try:
            msg = await self._messages.add_message(
                session_id=session_id,
                role=role,
                content=content,
                input_mode=input_mode,
                interrupted=bool(body.get("interrupted", False)),
                audio_duration_s=body.get("audio_duration_s"),
                token_count=body.get("token_count"),
                model=body.get("model"),
                latency_ms=body.get("latency_ms"),
                media_id=body.get("media_id"),
            )
        except Exception as e:
            logger.exception("add_message failed for session %s", session_id)
            return json_error(f"Failed to add message: {e}", 500)

        return web.json_response(msg, status=201)
