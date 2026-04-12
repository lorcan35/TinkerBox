"""Lightweight MCP client for connecting to MCP servers.

Supports stdio and HTTP transport. Discovers tools from connected servers
and bridges them into the Dragon ToolRegistry.
"""

import asyncio
import json
import logging
import subprocess
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


class MCPClient:
    """Connect to an MCP server and discover its tools."""

    def __init__(
        self,
        name: str,
        command: Optional[str] = None,
        url: Optional[str] = None,
        token: Optional[str] = None,
    ):
        self.name = name
        self.command = command  # For stdio transport: "npx @modelcontextprotocol/server-weather"
        self.url = url          # For HTTP transport: "http://localhost:8123/mcp"
        self.token = token
        self._process: Optional[asyncio.subprocess.Process] = None
        self._tools: list[dict] = []

    async def connect(self):
        """Connect to the MCP server and discover tools."""
        if self.url:
            await self._connect_http()
        elif self.command:
            await self._connect_stdio()

    async def _connect_http(self):
        """Connect via HTTP/SSE transport."""
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                # Initialize session
                async with session.post(
                    self.url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26",
                            "clientInfo": {
                                "name": "TinkerClaw Dragon",
                                "version": "0.1.0",
                            },
                            "capabilities": {},
                        },
                    },
                    headers=headers,
                ) as r:
                    if r.status == 200:
                        await r.json()
                        logger.info("MCP %s: initialized", self.name)
                    else:
                        logger.warning(
                            "MCP %s: initialize failed (HTTP %d)", self.name, r.status
                        )
                        return

                # List tools
                async with session.post(
                    self.url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/list",
                        "params": {},
                    },
                    headers=headers,
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        self._tools = data.get("result", {}).get("tools", [])
                        logger.info(
                            "MCP %s: discovered %d tools", self.name, len(self._tools)
                        )
                    else:
                        logger.warning(
                            "MCP %s: tools/list failed (HTTP %d)", self.name, r.status
                        )
        except Exception as e:
            logger.warning("MCP %s: connection failed: %s", self.name, e)

    async def _connect_stdio(self):
        """Connect via stdio transport (subprocess).

        Not yet implemented -- HTTP is the priority transport.
        """
        logger.info("MCP %s: stdio transport not yet implemented", self.name)

    async def call_tool(self, tool_name: str, args: dict) -> dict:
        """Execute a tool on the MCP server."""
        if not self.url:
            return {"error": "Not connected (no URL)"}
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            ) as session:
                async with session.post(
                    self.url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": tool_name, "arguments": args},
                    },
                    headers=headers,
                ) as r:
                    data = await r.json()
                    return data.get("result", data)
        except Exception as e:
            logger.exception("MCP %s: tool call %s failed", self.name, tool_name)
            return {"error": str(e)}

    async def disconnect(self):
        """Clean up resources."""
        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except Exception:
                self._process.kill()
            self._process = None

    @property
    def tools(self) -> list[dict]:
        return self._tools
