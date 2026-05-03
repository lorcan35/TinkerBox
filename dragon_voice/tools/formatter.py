"""Tool descriptor formatter — compact + full LLM-prompt formats.

Wave 23 SOLID-audit follow-up — twenty-fourth sub-extract;
second slice from `tools/registry.py` (audit SRP-5: registry +
3-dialect parser + 2-format formatter all bundled in one
class).  PR #249 already extracted the parser; this completes
SRP-5 by extracting the formatter.

The formatter renders a list of registered tools into a system-
prompt block suitable for injection into LLM context.  Two
flavours:

  * **Compact** — minimal format for small local models
    (qwen3:1.7b etc).  Fewer tokens, simpler structure, only
    the priority tools — small models confuse easily on long
    tool lists.
  * **Full** — rich format for capable cloud models with
    multiple worked examples.

Pre-extract these were 70 LOC of `_format_compact` /
`_format_full` methods on ToolRegistry, reaching back into
`self._tools`.  Now lives as free functions taking the tool
list explicitly; the registry keeps a thin `format_for_llm`
forwarding method.

## API

```python
text = format_for_llm(tools, compact=False)
```

`tools` is the iterable of registered Tool instances (typically
`registry._tools.values()`).  `compact=True` switches to the
small-model format.

## Why a separate module

Formatter concerns evolve independently of registry/parser:
adding a new dialect to the prompt block, tuning the priority-
tool list (issue #134 added schedule_reminder), or experimenting
with template variations all happen here without touching the
registry or parser modules.

## Priority-tool list (issue #134 closure)

`schedule_reminder` was missing from the compact priority list
pre-#134 — local LLM hallucinated "no such tool available" when
the user asked for a reminder.  The list is module-level here
so the contract is greppable; future additions are a one-line
edit visible to the audit doc.
"""
from __future__ import annotations

from typing import Iterable

from dragon_voice.tools.base import Tool


# Compact-format priority tool list.  Only these get included
# in the small-model prompt — wider lists confuse qwen3:1.7b
# etc into picking the wrong tool or refusing entirely.
#
# Issue #134: `schedule_reminder` was missing — local LLM
# hallucinated "no such tool available" when the user asked
# for a reminder.  Adding ~30 tokens to the system prompt is a
# worthwhile trade for unlocking a high-value capability.
COMPACT_PRIORITY_TOOLS: tuple[str, ...] = (
    "web_search",
    "datetime",
    "remember",
    "recall",
    "calculator",
    "schedule_reminder",
)

# Fallback when none of the priority tools are registered (e.g.
# a test harness with a custom tool set).  Keeps the compact
# block from being empty + structurally broken.
_COMPACT_FALLBACK_LIMIT = 4


def format_for_llm(
    tools: Iterable[Tool],
    *,
    compact: bool = False,
) -> str:
    """Render the registered tools into a system-prompt block.

    Args:
        tools: Iterable of Tool instances to render (typically
            `registry._tools.values()`).
        compact: If True, use the small-model minimal format
            (priority tools only, single example per tool).
            If False, use the full format with multi-example
            block.

    Returns:
        The formatted block ready to inject into the system
        prompt.  Empty string when `tools` is empty (caller
        should still inject — empty block is harmless).
    """
    tools_list = list(tools)
    if not tools_list:
        return ""

    if compact:
        return _format_compact(tools_list)
    return _format_full(tools_list)


def _format_compact(tools: list[Tool]) -> str:
    """Minimal tool format for small models (qwen3:1.7b etc).

    Uses fewer tokens and simpler structure to avoid confusing
    small models.  Only shows the priority tools to reduce
    context bloat.
    """
    selected = [t for t in tools if t.name in COMPACT_PRIORITY_TOOLS]
    if not selected:
        # No priority tools registered — fall back to the first
        # few tools so the block isn't empty/broken.
        selected = tools[:_COMPACT_FALLBACK_LIMIT]

    lines = ["\n[TOOLS]"]
    lines.append("Format: <tool>NAME</tool><args>{JSON}</args>")
    for tool in selected:
        params = tool.parameters_schema.get("properties", {})
        required = tool.parameters_schema.get("required", [])
        req_keys = [k for k in params if k in required]
        lines.append(f"- {tool.name}: {tool.description}")
        if req_keys:
            lines.append(
                f'  Example: <tool>{tool.name}</tool>'
                f'<args>{{"{req_keys[0]}": "..."}}</args>'
            )
    lines.append("Only use tools when needed. Most questions don't need tools.")
    lines.append("[/TOOLS]")
    return "\n".join(lines)


def _format_full(tools: list[Tool]) -> str:
    """Full tool format for capable cloud models."""
    lines = ["\n[TOOLS]"]
    lines.append("You can use tools by outputting EXACTLY this format:")
    lines.append('<tool>TOOLNAME</tool><args>{"key": "value"}</args>')
    lines.append("")
    lines.append("Available tools:")
    for tool in tools:
        params = tool.parameters_schema.get("properties", {})
        lines.append(f"  {tool.name}: {tool.description}")
        if params:
            lines.append(
                "    Args: {"
                + ", ".join(
                    f'"{k}": {v.get("type", "")}'
                    for k, v in params.items()
                )
                + "}"
            )

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
