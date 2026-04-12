"""Web search tool — SearXNG (self-hosted) with DuckDuckGo fallback."""

import asyncio
import logging

import aiohttp

from dragon_voice.tools.base import Tool

logger = logging.getLogger(__name__)


class WebSearchTool(Tool):
    """Search the web. Uses SearXNG if configured, falls back to DuckDuckGo."""

    def __init__(self, searxng_url: str = ""):
        self._searxng_url = searxng_url.rstrip("/") if searxng_url else ""

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

        # Try SearXNG first (self-hosted, private, metasearch)
        if self._searxng_url:
            try:
                results = await self._searxng_search(query, max_results)
                logger.info("SearXNG search: %r → %d results", query, len(results))
                return {"query": query, "results": results, "engine": "searxng"}
            except Exception as e:
                logger.warning("SearXNG failed (%s), falling back to DuckDuckGo", e)

        # Fallback: DuckDuckGo (no API key, works anywhere)
        try:
            results = await asyncio.to_thread(self._ddg_search, query, max_results)
            logger.info("DuckDuckGo search: %r → %d results", query, len(results))
            return {"query": query, "results": results, "engine": "duckduckgo"}
        except Exception as e:
            logger.exception("Web search failed for: %s", query)
            return {"error": f"Search failed: {e}"}

    async def _searxng_search(self, query: str, max_results: int) -> list[dict]:
        """Query a SearXNG instance via its JSON API."""
        params = {
            "q": query,
            "format": "json",
            "engines": "google,bing,duckduckgo",
        }
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{self._searxng_url}/search", params=params) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"SearXNG returned {resp.status}")
                data = await resp.json()
                return [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "snippet": r.get("content", "")[:300],
                    }
                    for r in data.get("results", [])[:max_results]
                ]

    def _ddg_search(self, query: str, max_results: int) -> list[dict]:
        """Synchronous DuckDuckGo search (run in thread)."""
        try:
            from ddgs import DDGS
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
