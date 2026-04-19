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


class ForgetFactTool(Tool):
    """Delete a stored fact.  Gauntlet G9: honors "forget that X" requests.

    Two-step auth gate:
      - Called without confirm=True, it searches for the best-matching fact
        and returns {"match": fact, "requires_confirm": True}.  The LLM is
        expected to read this back to the user ("I'd forget 'X' -- confirm?")
        before calling again with confirm=True.
      - Called with confirm=True, it deletes by fact_id and returns success.
    """

    def __init__(self, memory_service) -> None:
        self._memory = memory_service

    @property
    def name(self) -> str:
        return "forget_fact"

    @property
    def description(self) -> str:
        return (
            "Forget a previously remembered fact. FIRST call with just "
            "'query' to find the match; read it back to the user for "
            "confirmation; THEN call again with 'fact_id' + confirm=true "
            "to actually delete."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural description of the fact to forget "
                                   "(e.g. 'that I'm allergic to peanuts').",
                },
                "fact_id": {
                    "type": "string",
                    "description": "Exact id returned by a prior search "
                                   "(use only on the confirmation call).",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "Set to true ONLY after the user has "
                                   "explicitly agreed to forget the match.",
                },
            },
            "required": [],
        }

    async def execute(self, args: dict) -> dict:
        fact_id = (args.get("fact_id") or "").strip()
        confirm = bool(args.get("confirm"))
        query = (args.get("query") or "").strip()

        # Confirmation call: fact_id + confirm=true → actually delete.
        if fact_id and confirm:
            ok = await self._memory.delete_fact(fact_id)
            if ok:
                logger.info("Fact forgotten: %s", fact_id)
                return {"deleted": True, "id": fact_id}
            return {"deleted": False, "error": f"no fact with id {fact_id}"}

        # Lookup call: find best match, return for confirmation.
        if not query and not fact_id:
            return {"error": "query or fact_id is required"}

        if query:
            hits = await self._memory.search_facts(query, limit=3)
            if not hits:
                return {"match": None, "message": "no matching fact found"}
            top = hits[0]
            return {
                "match": {"id": top.get("id"), "content": top.get("content")},
                "alternatives": [
                    {"id": h.get("id"), "content": h.get("content")}
                    for h in hits[1:]
                ],
                "requires_confirm": True,
                "message": (
                    "Found a match. Read it back to the user and call "
                    "forget_fact again with fact_id + confirm=true to delete."
                ),
            }

        # fact_id supplied without confirm → refuse.
        return {
            "error": "refusing to delete without confirm=true",
            "id": fact_id,
        }
