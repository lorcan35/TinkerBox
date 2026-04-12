"""Tool registry: register, parse, execute tools."""

import json
import logging
import re
import time
from typing import Optional

from dragon_voice.tools.base import Tool

logger = logging.getLogger(__name__)

# XML-style markers for tool calls in LLM output
# Tolerant regex: handles stray > after JSON, missing </args>, extra whitespace
TOOL_PATTERN = re.compile(r'<tool>(\w+)</tool>\s*<args>\s*({.*?})\s*>?\s*</args>', re.DOTALL)
# Fallback: if </args> is missing entirely, grab JSON after <args>
TOOL_PATTERN_LOOSE = re.compile(r'<tool>(\w+)</tool>\s*<args>\s*({[^<]*})', re.DOTALL)


class ToolRegistry:
    """Central registry for all available tools."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool by name."""
        self._tools[tool.name] = tool
        logger.info("Tool registered: %s", tool.name)

    def get(self, name: str) -> Optional[Tool]:
        """Get a tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> list[dict]:
        """List all registered tools as dicts."""
        return [t.to_dict() for t in self._tools.values()]

    async def execute(self, name: str, args: dict) -> dict:
        """Execute a tool by name. Returns result dict with metadata."""
        tool = self._tools.get(name)
        if not tool:
            return {"error": f"Tool '{name}' not found"}

        t0 = time.monotonic()
        try:
            result = await tool.execute(args)
            execution_ms = (time.monotonic() - t0) * 1000
            logger.info("Tool %s executed in %.0fms", name, execution_ms)
            return {
                "tool": name,
                "result": result,
                "execution_ms": round(execution_ms),
            }
        except Exception as e:
            logger.exception("Tool %s failed", name)
            return {"tool": name, "error": str(e)}

    def parse_tool_calls(self, text: str) -> list[dict]:
        """Parse tool calls from LLM output text.

        Looks for <tool>name</tool><args>{"key": "value"}</args> patterns.
        Tolerant of small model quirks (stray >, missing </args>, etc).
        Returns list of {"tool": name, "args": dict}.
        """
        # Try strict pattern first, then loose fallback
        matches = TOOL_PATTERN.findall(text)
        if not matches:
            matches = TOOL_PATTERN_LOOSE.findall(text)
        calls = []
        for name, args_str in matches:
            # Clean up common small-model quirks
            args_str = args_str.strip().rstrip(">").strip()
            try:
                args = json.loads(args_str)
                calls.append({"tool": name, "args": args})
            except json.JSONDecodeError:
                logger.warning("Failed to parse tool args for %s: %s", name, args_str[:100])
        return calls

    def has_tool_call(self, text: str) -> bool:
        """Quick check if text contains a tool call marker."""
        return "<tool>" in text and "</tool>" in text

    def format_for_llm(self, compact: bool = False) -> str:
        """Format tool descriptions for injection into LLM system prompt.

        Args:
            compact: If True, use minimal format for small local models
                    (fewer tokens = faster generation, less confusion).
        """
        if not self._tools:
            return ""

        if compact:
            return self._format_compact()
        return self._format_full()

    def _format_compact(self) -> str:
        """Minimal tool format for small models (qwen3:1.7b etc).

        Uses fewer tokens and simpler structure to avoid confusing small models.
        Only shows the 4 most commonly used tools to reduce context bloat.
        """
        # Core tools that small models handle well
        priority_tools = ["web_search", "datetime", "remember", "recall", "calculator"]
        tools = [t for t in self._tools.values() if t.name in priority_tools]
        if not tools:
            tools = list(self._tools.values())[:4]

        lines = ["\n[TOOLS]"]
        lines.append("Format: <tool>NAME</tool><args>{JSON}</args>")
        for tool in tools:
            params = tool.parameters_schema.get("properties", {})
            required = tool.parameters_schema.get("required", [])
            req_keys = [k for k in params if k in required]
            lines.append(f"- {tool.name}: {tool.description}")
            if req_keys:
                lines.append(f'  Example: <tool>{tool.name}</tool><args>{{"{req_keys[0]}": "..."}}</args>')
        lines.append("Only use tools when needed. Most questions don't need tools.")
        lines.append("[/TOOLS]")
        return "\n".join(lines)

    def _format_full(self) -> str:
        """Full tool format for capable cloud models."""
        lines = ["\n[TOOLS]"]
        lines.append("You can use tools by outputting EXACTLY this format:")
        lines.append('<tool>TOOLNAME</tool><args>{"key": "value"}</args>')
        lines.append("")
        lines.append("Available tools:")
        for tool in self._tools.values():
            params = tool.parameters_schema.get("properties", {})
            lines.append(f"  {tool.name}: {tool.description}")
            if params:
                lines.append("    Args: {" + ", ".join(f'"{k}": {v.get("type","")}' for k,v in params.items()) + "}")

        lines.append("")
        lines.append("Examples:")
        lines.append('  <tool>web_search</tool><args>{"query": "weather today"}</args>')
        lines.append('  <tool>remember</tool><args>{"fact": "User likes pizza"}</args>')
        lines.append('  <tool>recall</tool><args>{"query": "user preferences"}</args>')
        lines.append('  <tool>calculator</tool><args>{"expression": "15% of 230"}</args>')
        lines.append('  <tool>weather</tool><args>{"location": "Tokyo"}</args>')
        lines.append("")
        lines.append("IMPORTANT: Only use a tool when genuinely needed. Most questions don't need tools.")
        lines.append("[/TOOLS]")

        return "\n".join(lines)
