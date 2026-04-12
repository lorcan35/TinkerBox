"""Memory facts CRUD and semantic search API routes."""

import logging

from aiohttp import web

from dragon_voice.api.utils import json_error, parse_json_body, parse_pagination, paginated_response
from dragon_voice.memory import MemoryService

logger = logging.getLogger(__name__)


class MemoryRoutes:
    def __init__(self, memory: MemoryService) -> None:
        self._memory = memory

    def register(self, app: web.Application) -> None:
        app.router.add_get("/api/v1/memory", self.list_facts)
        app.router.add_post("/api/v1/memory", self.store_fact)
        app.router.add_delete("/api/v1/memory/{fact_id}", self.delete_fact)
        app.router.add_post("/api/v1/memory/search", self.search_facts)

    async def list_facts(self, request: web.Request) -> web.Response:
        """GET /api/v1/memory?limit=50&offset=0"""
        limit, offset = parse_pagination(request)
        facts = await self._memory.list_facts(limit, offset)
        return paginated_response(facts, limit, offset)

    async def store_fact(self, request: web.Request) -> web.Response:
        """POST /api/v1/memory — store a fact

        Request: {"content": "User prefers dark mode", "source": "manual"}
        """
        body, err = await parse_json_body(request)
        if err:
            return err
        content = body.get("content", "").strip()
        if not content:
            return json_error("'content' field is required")
        source = body.get("source", "manual")
        result = await self._memory.store_fact(content, source=source)
        return web.json_response(result, status=201)

    async def delete_fact(self, request: web.Request) -> web.Response:
        """DELETE /api/v1/memory/{fact_id}"""
        fact_id = request.match_info["fact_id"]
        deleted = await self._memory.delete_fact(fact_id)
        if not deleted:
            return json_error("Fact not found", 404)
        return web.json_response({"status": "deleted", "id": fact_id})

    async def search_facts(self, request: web.Request) -> web.Response:
        """POST /api/v1/memory/search — semantic search

        Request: {"query": "user preferences", "limit": 10}
        """
        body, err = await parse_json_body(request)
        if err:
            return err
        query = body.get("query", "").strip()
        if not query:
            return json_error("'query' field is required")
        limit = body.get("limit", 10)
        results = await self._memory.search_facts(query, limit=limit)
        return web.json_response({"results": results, "query": query})
