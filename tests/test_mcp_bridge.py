"""
Audit D9/K9 regression: MCP bridge was "never exercised in tests".
This is a minimal sanity probe — we don't exercise real MCP servers
(they'd need to be running), we just make sure the module imports
cleanly, constructors work, and the class surface area hasn't drifted.

If someone ever deletes mcp/bridge.py or renames MCPToolBridge, this
test catches it.
"""

import pytest


def test_mcp_module_imports() -> None:
    """Import chain doesn't throw on a clean interpreter."""
    from dragon_voice.mcp import bridge, client  # noqa: F401
    from dragon_voice.mcp.bridge import MCPToolBridge, bridge_mcp_server
    from dragon_voice.mcp.client import MCPClient

    # Smoke-check the public surface the registry expects.
    assert callable(bridge_mcp_server)
    assert callable(MCPToolBridge)
    assert callable(MCPClient)


def test_mcp_client_construction() -> None:
    """MCPClient(name=..., url=...) should build without touching the network."""
    from dragon_voice.mcp.client import MCPClient

    c = MCPClient(name="test", url="http://127.0.0.1:9999/mcp")
    assert c.name == "test"
    # Surface area the bridge uses.
    assert hasattr(c, "connect")
    assert hasattr(c, "call_tool")
    assert hasattr(c, "disconnect")
    assert hasattr(c, "tools")


def test_mcp_tool_bridge_wraps_tool_dict() -> None:
    """MCPToolBridge exposes a valid Tool interface for a synthetic tool dict."""
    from dragon_voice.mcp.client import MCPClient
    from dragon_voice.mcp.bridge import MCPToolBridge

    client = MCPClient(name="fake_server", url="http://localhost:0/mcp")
    tool_dict = {
        "name": "echo",
        "description": "Echo input back.",
        "inputSchema": {"type": "object", "properties": {"msg": {"type": "string"}}},
    }
    tool = MCPToolBridge(client, tool_dict)

    # Registry-facing properties
    assert tool.name == "fake_server_echo"
    assert tool.description == "Echo input back."
    assert tool.parameters_schema["type"] == "object"
    assert "msg" in tool.parameters_schema["properties"]


@pytest.mark.asyncio
async def test_mcp_client_connect_gracefully_fails() -> None:
    """Connecting to a definitely-unreachable URL must NOT crash the
    interpreter or hang forever. The client may either raise or log +
    return with an empty tools() list; both are acceptable soft-fail
    behaviours."""
    from dragon_voice.mcp.client import MCPClient

    c = MCPClient(name="dead", url="http://127.0.0.1:1/mcp")  # port 1 = not listening
    try:
        await c.connect()
    except Exception:
        pass  # raise-on-fail path
    # Whichever path it took, tools must not explode and must be empty
    # (no real server to enumerate from). Attribute-based access — some
    # versions of the client expose `tools` as a property, others as a
    # method.
    t = c.tools() if callable(c.tools) else c.tools
    assert t == []
