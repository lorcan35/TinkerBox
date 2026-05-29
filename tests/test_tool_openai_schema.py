"""Unit tests for ToolRegistry.openai_tools() — the native tool-calling
schema builder (SupportsNativeTools path). Pure, no network: CI-safe."""
from dragon_voice.tools.base import Tool
from dragon_voice.tools.registry import ToolRegistry


class _Echo(Tool):
    def __init__(self, name, desc, schema):
        self._n, self._d, self._s = name, desc, schema

    @property
    def name(self):
        return self._n

    @property
    def description(self):
        return self._d

    @property
    def parameters_schema(self):
        return self._s

    async def execute(self, args):
        return {"ok": True, "args": args}


def test_openai_tools_shape():
    reg = ToolRegistry()
    reg.register(_Echo("gmail_search", "Search emails", {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }))
    tools = reg.openai_tools()
    assert len(tools) == 1
    fn = tools[0]
    assert fn["type"] == "function"
    assert fn["function"]["name"] == "gmail_search"
    assert fn["function"]["description"] == "Search emails"
    assert fn["function"]["parameters"]["properties"]["query"]["type"] == "string"


def test_openai_tools_empty_schema_defaults_to_object():
    reg = ToolRegistry()
    reg.register(_Echo("calendar_today", "Get today's events", {}))
    tools = reg.openai_tools()
    params = tools[0]["function"]["parameters"]
    assert params["type"] == "object"
    assert params["properties"] == {}


def test_openai_tools_lists_every_registered_tool():
    reg = ToolRegistry()
    for n in ("a", "b", "c"):
        reg.register(_Echo(n, n.upper(), {"type": "object", "properties": {}}))
    names = {t["function"]["name"] for t in reg.openai_tools()}
    assert names == {"a", "b", "c"}
