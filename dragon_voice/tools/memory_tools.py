"""Memory tools: remember and recall facts."""

import logging

from dragon_voice.tools.base import Tool

logger = logging.getLogger(__name__)


class StoreFactTool(Tool):
    """Store a fact about the user for future reference."""

    def __init__(self, memory_service) -> None:
        self._memory = memory_service

    @property
    def name(self) -> str:
        return "remember"

    @property
    def description(self) -> str:
        return "Save a fact or preference about the user for future conversations"

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "fact": {
                    "type": "string",
                    "description": "The fact to remember (e.g., 'User is allergic to peanuts')",
                },
            },
            "required": ["fact"],
        }

    async def execute(self, args: dict) -> dict:
        fact = args.get("fact", "").strip()
        if not fact:
            return {"error": "fact is required"}

        result = await self._memory.store_fact(fact, source="tool")
        return {"stored": True, "id": result["id"], "fact": fact}


class RecallFactsTool(Tool):
    """Search memory for relevant information."""

    def __init__(self, memory_service) -> None:
        self._memory = memory_service

    @property
    def name(self) -> str:
        return "recall"

    @property
    def description(self) -> str:
        return "Search your memory for relevant information about the user or past conversations"

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to search for in memory",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 5)",
                },
            },
            "required": ["query"],
        }

    async def execute(self, args: dict) -> dict:
        query = args.get("query", "").strip()
        limit = args.get("limit", 5)
        if not query:
            return {"error": "query is required"}

        results = await self._memory.search_facts(query, limit=limit)
        return {"query": query, "facts": results}
