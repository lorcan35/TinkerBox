"""REST API routes for notes CRUD and search."""

import json
import logging

from aiohttp import web

from dragon_voice.notes.service import NotesService
from dragon_voice.notes.db import Note

logger = logging.getLogger(__name__)


def setup_routes(app: web.Application, service: NotesService) -> None:
    """Register all notes API routes on the aiohttp app."""
    handler = NotesAPI(service)
    app.router.add_post("/api/notes", handler.create_note)
    app.router.add_get("/api/notes", handler.list_notes)
    app.router.add_get("/api/notes/{note_id}", handler.get_note)
    app.router.add_put("/api/notes/{note_id}", handler.update_note)
    app.router.add_delete("/api/notes/{note_id}", handler.delete_note)
    app.router.add_post("/api/notes/search", handler.search_notes)
    app.router.add_post("/api/notes/from-audio", handler.create_from_audio)


class NotesAPI:
    def __init__(self, service: NotesService) -> None:
        self._svc = service

    async def create_note(self, request: web.Request) -> web.Response:
        """POST /api/notes — create from text."""
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        text = body.get("text", "").strip()
        title = body.get("title", "").strip()

        if not text:
            return web.json_response({"error": "text is required"}, status=400)

        note = await self._svc.create_from_text(text, title)
        return web.json_response(note.to_dict(), status=201)

    async def list_notes(self, request: web.Request) -> web.Response:
        """GET /api/notes?limit=50&offset=0"""
        limit = int(request.query.get("limit", "50"))
        offset = int(request.query.get("offset", "0"))
        notes, total = self._svc.list_notes(limit, offset)
        return web.json_response({
            "notes": [n.to_dict() for n in notes],
            "total": total,
            "limit": limit,
            "offset": offset,
        })

    async def get_note(self, request: web.Request) -> web.Response:
        """GET /api/notes/{note_id}"""
        note_id = request.match_info["note_id"]
        note = self._svc.get_note(note_id)
        if not note:
            return web.json_response({"error": "Not found"}, status=404)
        return web.json_response(note.to_dict())

    async def update_note(self, request: web.Request) -> web.Response:
        """PUT /api/notes/{note_id}"""
        note_id = request.match_info["note_id"]
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        allowed = {"title", "transcript", "summary", "tags"}
        updates = {k: v for k, v in body.items() if k in allowed}
        if not updates:
            return web.json_response({"error": "No valid fields to update"}, status=400)

        note = self._svc.update_note(note_id, updates)
        if not note:
            return web.json_response({"error": "Not found"}, status=404)
        return web.json_response(note.to_dict())

    async def delete_note(self, request: web.Request) -> web.Response:
        """DELETE /api/notes/{note_id}"""
        note_id = request.match_info["note_id"]
        deleted = self._svc.delete_note(note_id)
        if not deleted:
            return web.json_response({"error": "Not found"}, status=404)
        return web.json_response({"status": "deleted", "id": note_id})

    async def search_notes(self, request: web.Request) -> web.Response:
        """POST /api/notes/search — semantic search."""
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        query = body.get("query", "").strip()
        if not query:
            return web.json_response({"error": "query is required"}, status=400)

        limit = body.get("limit", 10)
        results = await self._svc.search(query, limit)
        return web.json_response({"results": results, "query": query})

    async def create_from_audio(self, request: web.Request) -> web.Response:
        """POST /api/notes/from-audio — upload raw PCM audio, get a note back."""
        body = await request.read()
        if not body:
            return web.json_response({"error": "Empty body"}, status=400)

        sample_rate = int(request.query.get("sample_rate", "16000"))
        note = await self._svc.create_from_audio(body, sample_rate)
        return web.json_response(note.to_dict(), status=201)
