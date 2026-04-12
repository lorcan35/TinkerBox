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

        response = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Access-Control-Allow-Origin": "*",
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
