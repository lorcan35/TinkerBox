"""Tool registry: register + execute tools.

Wave 23 SOLID-audit follow-up — parser extracted to
`dragon_voice.tools.parser` (PR #249, audit SRP-5 first slice).
This module now owns just the registry + execution + formatter
responsibilities; the three-dialect XML/bracket parser lives
next door and is invoked via thin forwarding methods on
`ToolRegistry` for backward compat with the 16+ existing call
sites.
"""

import logging
import time
from typing import Optional

from dragon_voice.tools.base import Tool

# Re-export parser symbols for backward compat with callers that
# imported them from registry directly (e.g.
# `from dragon_voice.tools.registry import TOOL_PATTERN`).
from dragon_voice.tools.parser import (  # noqa: F401  (re-exports)
    TOOL_PATTERN,
    TOOL_PATTERN_LOOSE,
    has_tool_call as _has_tool_call,
    parse_tool_calls as _parse_tool_calls,
    parse_tool_calls_with_errors as _parse_tool_calls_with_errors,
)

logger = logging.getLogger(__name__)


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

        # Wave 12 — record on the cross-session agent log here so
        # the feed captures every invocation: WS conversations,
        # direct REST `/api/v1/tools/.../execute`, and dashboard
        # triggers all funnel through this method.  Wrapped in
        # try/except so an instrumentation failure never breaks a
        # live tool call.
        try:
            from dragon_voice.api.agent_log import (
                record_call as _agent_log_call,
            )
            _agent_log_call(name, args)
        except Exception:  # noqa: BLE001
            logger.debug("agent_log record_call suppressed", exc_info=True)

        t0 = time.monotonic()
        try:
            result = await tool.execute(args)
            execution_ms = (time.monotonic() - t0) * 1000
            logger.info("Tool %s executed in %.0fms", name, execution_ms)
            try:
                from dragon_voice.api.agent_log import (
                    record_result as _agent_log_result,
                )
                _agent_log_result(name, result, round(execution_ms))
            except Exception:  # noqa: BLE001
                logger.debug("agent_log record_result suppressed", exc_info=True)
            return {
                "tool": name,
                "result": result,
                "execution_ms": round(execution_ms),
            }
        except Exception as e:
            logger.exception("Tool %s failed", name)
            try:
                from dragon_voice.api.agent_log import (
                    record_result as _agent_log_result,
                )
                _agent_log_result(name, {"error": str(e)}, None)
            except Exception:  # noqa: BLE001
                logger.debug("agent_log record_result(err) suppressed", exc_info=True)
            return {"tool": name, "error": str(e)}

    def parse_tool_calls(self, text: str) -> list[dict]:
        """Parse tool calls from LLM output text.

        Backward-compatible wrapper around the
        ``parser.parse_tool_calls_with_errors`` free function.  The
        16+ existing call sites that don't surface parse errors
        consume this signature; new code should prefer the
        ``with_errors`` sibling so γ2-M1 (#104) parse failures
        reach the user via tool_parse_failed events.
        """
        return _parse_tool_calls(text, registered_names=self._tools.keys())

    def parse_tool_calls_with_errors(
        self, text: str
    ) -> tuple[list[dict], list[dict]]:
        """Like :meth:`parse_tool_calls`, but also returns parse errors.

        γ2-M1 (issue #104, refs #89, refs #101) — see
        ``dragon_voice.tools.parser`` for the full implementation.
        Pre-fix the silent JSON-decode swallow surfaced as
        user-invisible agentic failure (LLM emits tool call,
        parser fails, tool never fires, user sees empty reply).
        """
        return _parse_tool_calls_with_errors(
            text, registered_names=self._tools.keys(),
        )

    def has_tool_call(self, text: str) -> bool:
        """Quick check if text contains a tool call marker.
        Accepts all three dialects the parser understands.

        Forwards to ``parser.has_tool_call`` with the registered
        tool-name set so dialect 3 doesn't false-fire on prose
        like ``[note]`` quoted in a chat reply.
        """
        return _has_tool_call(text, registered_names=self._tools.keys())

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
        Only shows the most commonly used tools to reduce context bloat.
        """
        # Core tools that small models handle well.  Issue #134:
        # `schedule_reminder` was missing — local LLM hallucinated
        # "no such tool available" when the user asked for a reminder.
        # Adding ~30 tokens to the system prompt is a worthwhile
        # trade for unlocking a high-value capability.
        priority_tools = [
            "web_search", "datetime", "remember", "recall",
            "calculator", "schedule_reminder",
        ]
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
