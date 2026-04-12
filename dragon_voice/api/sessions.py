"""Session CRUD + lifecycle API routes."""

import json
import logging

from aiohttp import web

from dragon_voice.api.utils import json_error, paginated_response, parse_pagination, parse_json_body
from dragon_voice.db import Database
from dragon_voice.sessions import SessionManager
from dragon_voice.messages import MessageStore
from dragon_voice.conversation import ConversationEngine

logger = logging.getLogger(__name__)


class SessionRoutes:
    def __init__(self, db: Database, session_mgr: SessionManager,
                 message_store: MessageStore, conversation: ConversationEngine | None = None) -> None:
        self._db = db
        self._session_mgr = session_mgr
        self._messages = message_store
        self._conversation = conversation

    def register(self, app: web.Application) -> None:
        app.router.add_get("/api/v1/sessions", self.list_sessions)
        app.router.add_post("/api/v1/sessions", self.create_session)
        app.router.add_get("/api/v1/sessions/{session_id}", self.get_session)
        app.router.add_post("/api/v1/sessions/{session_id}/end", self.end_session)
        # Sprint 1: new lifecycle endpoints
        app.router.add_post("/api/v1/sessions/{session_id}/resume", self.resume_session)
        app.router.add_post("/api/v1/sessions/{session_id}/pause", self.pause_session)
        app.router.add_patch("/api/v1/sessions/{session_id}", self.update_session)
        app.router.add_get("/api/v1/sessions/{session_id}/context", self.get_context)

    async def list_sessions(self, request: web.Request) -> web.Response:
        """GET /api/v1/sessions?device_id=&status=&limit=&offset="""
        device_id = request.query.get("device_id")
        status = request.query.get("status")
        limit, offset = parse_pagination(request)
        sessions = await self._session_mgr.list_sessions(
            device_id=device_id, status=status, limit=limit, offset=offset
        )
        return paginated_response(sessions, limit, offset)

    async def create_session(self, request: web.Request) -> web.Response:
        """POST /api/v1/sessions"""
        body, err = await parse_json_body(request)
        if err:
            return err
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
            return json_error("Session not found", 404)
        return web.json_response(session)

    async def end_session(self, request: web.Request) -> web.Response:
        """POST /api/v1/sessions/{session_id}/end"""
        session_id = request.match_info["session_id"]
        session = await self._session_mgr.get_session(session_id)
        if not session:
            return json_error("Session not found", 404)
        await self._session_mgr.end_session(session_id)
        return web.json_response({"status": "ended", "session_id": session_id})

    # ── Sprint 1: New lifecycle endpoints ──

    async def resume_session(self, request: web.Request) -> web.Response:
        """POST /api/v1/sessions/{session_id}/resume"""
        session_id = request.match_info["session_id"]
        session = await self._session_mgr.resume_session(session_id)
        if not session:
            existing = await self._session_mgr.get_session(session_id)
            if not existing:
                return json_error("Session not found", 404)
            return json_error("Session already ended, cannot resume", 409)
        return web.json_response(session)

    async def pause_session(self, request: web.Request) -> web.Response:
        """POST /api/v1/sessions/{session_id}/pause"""
        session_id = request.match_info["session_id"]
        session = await self._session_mgr.get_session(session_id)
        if not session:
            return json_error("Session not found", 404)
        await self._session_mgr.pause_session(session_id)
        return web.json_response({"status": "paused", "session_id": session_id})

    async def update_session(self, request: web.Request) -> web.Response:
        """PATCH /api/v1/sessions/{session_id} — update title/system_prompt/metadata/config"""
        session_id = request.match_info["session_id"]
        session = await self._session_mgr.get_session(session_id)
        if not session:
            return json_error("Session not found", 404)

        body, err = await parse_json_body(request)
        if err:
            return err

        allowed = {"title", "system_prompt", "metadata", "config"}
        updates = {k: v for k, v in body.items() if k in allowed}
        if not updates:
            return json_error("No valid fields to update (allowed: title, system_prompt, metadata, config)")

        await self._db.update_session(session_id, **updates)
        updated = await self._session_mgr.get_session(session_id)
        return web.json_response(updated)

    async def get_context(self, request: web.Request) -> web.Response:
        """GET /api/v1/sessions/{session_id}/context — formatted LLM context with memory + tools

        Optional query: ?query=text — enriches context with relevant memories for that query
        """
        session_id = request.match_info["session_id"]
        session = await self._session_mgr.get_session(session_id)
        if not session:
            return json_error("Session not found", 404)

        max_messages = int(request.query.get("max_messages", "20"))
        query = request.query.get("query", "")

        # Use conversation engine's _build_context if available (includes memory + tools)
        if self._conversation and query:
            context = await self._conversation._build_context(session_id, query)
        else:
            context = await self._messages.get_context(session_id, max_messages=max_messages)
            # Still inject tool descriptions if conversation engine has tools
            if self._conversation and self._conversation._tool_registry:
                tool_desc = self._conversation._tool_registry.format_for_llm()
                if tool_desc and context and context[0]["role"] == "system":
                    context[0]["content"] += "\n" + tool_desc

        count = await self._messages.count_messages(session_id)
        return web.json_response({
            "session_id": session_id,
            "context": context,
            "message_count": count,
        })
