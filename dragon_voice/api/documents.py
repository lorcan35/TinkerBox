"""Document ingestion and listing API routes."""

import logging

from aiohttp import web

from dragon_voice.api.utils import json_error, parse_json_body, parse_pagination, paginated_response
from dragon_voice.memory import MemoryService

logger = logging.getLogger(__name__)


class DocumentRoutes:
    def __init__(self, memory: MemoryService) -> None:
        self._memory = memory

    def register(self, app: web.Application) -> None:
        app.router.add_post("/api/v1/documents", self.ingest_document)
        app.router.add_get("/api/v1/documents", self.list_documents)
        app.router.add_delete("/api/v1/documents/{doc_id}", self.delete_document)
        app.router.add_post("/api/v1/documents/search", self.search_documents)

    async def ingest_document(self, request: web.Request) -> web.Response:
        """POST /api/v1/documents — ingest text document

        Request: {"title": "Project Notes", "content": "...", "metadata": {}}
        """
        body, err = await parse_json_body(request)
        if err:
            return err
        title = body.get("title", "Untitled")
        content = body.get("content", "").strip()
        if not content:
            return json_error("'content' field is required")
        metadata = body.get("metadata")
        result = await self._memory.ingest_document(title, content, metadata)
        return web.json_response(result, status=201)

    async def list_documents(self, request: web.Request) -> web.Response:
        """GET /api/v1/documents?limit=50&offset=0"""
        limit, offset = parse_pagination(request)
        docs = await self._memory.list_documents(limit, offset)
        return paginated_response(docs, limit, offset)

    async def delete_document(self, request: web.Request) -> web.Response:
        """DELETE /api/v1/documents/{doc_id}"""
        doc_id = request.match_info["doc_id"]
        deleted = await self._memory.delete_document(doc_id)
        if not deleted:
            return json_error("Document not found", 404)
        return web.json_response({"status": "deleted", "id": doc_id})

    async def search_documents(self, request: web.Request) -> web.Response:
        """POST /api/v1/documents/search — semantic search across document chunks

        Request: {"query": "...", "limit": 5}
        """
        body, err = await parse_json_body(request)
        if err:
            return err
        query = body.get("query", "").strip()
        if not query:
            return json_error("'query' field is required")
        limit = body.get("limit", 5)
        results = await self._memory.search_documents(query, limit=limit)
        return web.json_response({"results": results, "query": query})
