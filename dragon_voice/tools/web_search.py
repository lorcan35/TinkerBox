"""Web search tool using DuckDuckGo."""

import asyncio
import logging

from dragon_voice.tools.base import Tool

logger = logging.getLogger(__name__)


class WebSearchTool(Tool):
    """Search the web using DuckDuckGo (no API key needed)."""

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return "Search the web for current information, news, facts, or answers"

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results (default 3)",
                },
            },
            "required": ["query"],
        }

    async def execute(self, args: dict) -> dict:
        query = args.get("query", "")
        max_results = args.get("max_results", 3)

        if not query:
            return {"error": "query is required"}

        try:
            results = await asyncio.to_thread(self._search, query, max_results)
            return {"query": query, "results": results}
        except Exception as e:
            logger.exception("Web search failed for: %s", query)
            return {"error": f"Search failed: {e}"}

    def _search(self, query: str, max_results: int) -> list[dict]:
        """Synchronous DuckDuckGo search (run in thread)."""
        try:
            from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                raw = list(ddgs.text(query, max_results=max_results))
                return [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("href", ""),
                        "snippet": r.get("body", "")[:300],
                    }
                    for r in raw
                ]
        except ImportError:
            return [{"error": "duckduckgo-search not installed. Run: pip install duckduckgo-search"}]
