"""Bridge MCP server tools into the Dragon ToolRegistry."""

import logging

from dragon_voice.tools.base import Tool
from dragon_voice.mcp.client import MCPClient

logger = logging.getLogger(__name__)


class MCPToolBridge(Tool):
    """Wraps an MCP server tool as a Dragon Tool."""

    def __init__(self, mcp_client: MCPClient, mcp_tool: dict):
        self._client = mcp_client
        self._mcp_tool = mcp_tool

    @property
    def name(self) -> str:
        # Prefix with server name to avoid conflicts
        return f"{self._client.name}_{self._mcp_tool['name']}"

    @property
    def description(self) -> str:
        return self._mcp_tool.get("description", "")

    @property
    def parameters_schema(self) -> dict:
        return self._mcp_tool.get(
            "inputSchema", {"type": "object", "properties": {}}
        )

    async def execute(self, args: dict) -> dict:
        return await self._client.call_tool(self._mcp_tool["name"], args)


async def bridge_mcp_server(
    registry,
    name: str,
    url: str = None,
    command: str = None,
    token: str = None,
) -> int:
    """Connect to an MCP server and register all its tools.

    Returns the number of tools bridged.
    """
    client = MCPClient(name, command=command, url=url, token=token)
    await client.connect()
    count = 0
    for tool in client.tools:
        bridge = MCPToolBridge(client, tool)
        registry.register(bridge)
        count += 1
        logger.info("MCP %s: bridged tool '%s'", name, bridge.name)
    return count
