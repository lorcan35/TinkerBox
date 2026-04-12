"""Tool listing and execution API routes."""

import logging

from aiohttp import web

from dragon_voice.api.utils import json_error, parse_json_body
from dragon_voice.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class ToolRoutes:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def register(self, app: web.Application) -> None:
        app.router.add_get("/api/v1/tools", self.list_tools)
        app.router.add_post("/api/v1/tools/{name}/execute", self.execute_tool)

    async def list_tools(self, request: web.Request) -> web.Response:
        """GET /api/v1/tools — list all available tools"""
        return web.json_response({"tools": self._registry.list_tools()})

    async def execute_tool(self, request: web.Request) -> web.Response:
        """POST /api/v1/tools/{name}/execute — execute a tool

        Request: {"args": {"query": "weather today"}}
        """
        name = request.match_info["name"]
        tool = self._registry.get(name)
        if not tool:
            return json_error(f"Tool '{name}' not found", 404)

        body, err = await parse_json_body(request)
        if err:
            return err

        args = body.get("args", {})
        result = await self._registry.execute(name, args)

        if "error" in result and result.get("tool") is None:
            return json_error(result["error"], 404)

        status = 200 if "error" not in result.get("result", {}) else 500
        return web.json_response(result, status=status)
