"""Tool registry: register + execute tools.

Wave 23 SOLID-audit follow-up (audit SRP-5):
  * Parser extracted to `dragon_voice.tools.parser` (PR #249).
  * Formatter extracted to `dragon_voice.tools.formatter`
    (PR #250).

This module now owns just the registry + execution
responsibilities; the parser and formatter live next door and
are invoked via thin forwarding methods on `ToolRegistry` for
backward compat with the 16+ existing call sites.
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
from dragon_voice.tools.formatter import format_for_llm as _format_for_llm

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

    def openai_tools(self) -> list[dict]:
        """Render registered tools as OpenAI-format function schemas.

        For the native tool-calling path (SupportsNativeTools): passed as
        `tools=[...]` to the llama-server OpenAI API. Each tool's
        `parameters_schema` is already a JSON-schema object, so this is a
        thin wrap. The whole registry is sent (not just the compact
        priority subset) — the native API gives the model a structured
        signal that doesn't bloat the prose context the way the prompt
        block does.
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters_schema or {
                        "type": "object",
                        "properties": {},
                    },
                },
            }
            for t in self._tools.values()
        ]

    def format_for_llm(self, compact: bool = False) -> str:
        """Format tool descriptions for injection into LLM system prompt.

        Forwards to ``formatter.format_for_llm`` with the
        registered Tool instances.  Compact format is for small
        local models (qwen3:1.7b etc — fewer tokens, priority
        tools only); full format is for capable cloud models.
        """
        return _format_for_llm(self._tools.values(), compact=compact)
