"""Tool registry: register, parse, execute tools."""

import json
import logging
import re
import time
from typing import Optional

from dragon_voice.tools.base import Tool

logger = logging.getLogger(__name__)

# XML-style markers for tool calls in LLM output.
#
# Three dialects are accepted:
#
#   1. LEGACY (TinkerBox system-prompt format):
#        <tool>NAME</tool><args>{"k": "v"}</args>
#      This is what Dragon's own system prompt instructs the model to emit.
#
#   2. STANDARD (industry-typical, Qwen-FC / Gemma-FC / distil-* / many
#      community function-calling fine-tunes):
#        <tool_call>{"name": "NAME", "arguments": {"k": "v"}}</tool_call>
#      Models trained for function-calling almost universally emit this
#      shape regardless of what our system prompt asks — that's what they
#      were fine-tuned on.  Accepting it here means we don't have to fight
#      the training prior of every FC model we want to run.
#
#   3. BRACKETED-NAME (xLAM quirk, surfaced during the dual-pipeline bench
#      in #80 / #81 — issue #82):
#        [NAME]{"k": "v"}</NAME>     — JSON args + matching close
#        [NAME]UPPERCASE_IDENT()     — empty args, function-call-style noise
#      The skill name IS the tag, with the open form using square brackets.
#      To avoid false positives on user prose like `[note]` quoted in a
#      chat reply, this dialect REQUIRES the name to be in the registered
#      tool set.  Without that gate, parser would fire on innocent text.
#
# All three shapes are extracted into the same `{"tool": name, "args": dict}`
# record, so downstream execute() + server eventing is format-agnostic.
#
# Tolerance knobs inherited from the prior implementation:
#   - Stray `>` after JSON, missing `</args>`, extra whitespace all handled.
#   - xLAM quirk on dialect 1: emits `[tool>` (left-bracket instead of
#     left-angle) on the opening tag — the anchor regex accepts either,
#     since the closing `</tool>` still disambiguates unambiguously.
#
# Tolerant regex: handles stray > after JSON, missing </args>, extra whitespace.
# The `[<\[]tool[>\]]` open-tag class accepts `<tool>`, `[tool>`, `<tool]`, AND
# `[tool]` — quantized small models (notably xLAM) emit broken open brackets
# semi-randomly; the closing `</tool>` is always intact so there's no ambiguity.
TOOL_PATTERN = re.compile(r'[<\[]tool[>\]](\w+)</tool>\s*<args>\s*({.*?})\s*>?\s*</args>', re.DOTALL)
# Fallback: if </args> is missing entirely, grab JSON after <args>
TOOL_PATTERN_LOOSE = re.compile(r'[<\[]tool[>\]](\w+)</tool>\s*<args>\s*({[^<]*})', re.DOTALL)

# Dialect 3 cheap pre-check — used by has_tool_call.  Matches `[name]`
# where `name` looks like a registered-tool identifier.  False positives
# at this stage are fine; parse_tool_calls validates against the tool
# registry before accepting.
_BRACKET_NAME_PRECHECK = re.compile(r"\[([a-z_][a-z0-9_]*)\]")
# Dialect 3 noise-args sanity check — the GETCURRENTDATEANDTIME() form
# from xLAM should look like a function name (uppercase identifier),
# not prose.  3-50 chars keeps real-world cases without exploding the
# match window.
_BRACKET_NOISE_IDENT = re.compile(r"^[A-Z][A-Z0-9_]{2,49}$")


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

        v4·D audit P2 fix: the regex `{.*?}` is non-greedy and will
        truncate at the first `}` it encounters -- a nested JSON value
        like {"filter": {"k": "v"}} lost its outer closer.  We now
        balance braces manually after the regex anchors the position.

        γ2-M1 (issue #104): pre-fix this method swallowed JSON-decode
        errors silently — `parse_tool_calls_with_errors` is the
        error-surfacing sibling.  This method preserves the original
        list-only signature for backward compatibility (16 existing
        unit tests + 2 conversation.py call sites).
        """
        calls, _errors = self.parse_tool_calls_with_errors(text)
        return calls

    def parse_tool_calls_with_errors(
        self, text: str
    ) -> tuple[list[dict], list[dict]]:
        """Like :meth:`parse_tool_calls`, but also returns parse errors.

        γ2-M1 (issue #104, refs #89, refs #101).  The Phase-2 audit
        identified the silent JSON-decode swallow as user-invisible
        agentic failure: the LLM emits a tool call, the parser fails
        to load the args, the tool never fires, and the user sees a
        generic / empty reply with no signal that anything was tried.

        Returns
        -------
        (calls, errors)
            ``calls`` is identical to what :meth:`parse_tool_calls`
            returns (list of ``{"tool": name, "args": dict}``).
            ``errors`` is a list of error records:

                {"dialect": 1|2|3,
                 "name": str | None,
                 "reason": "json_decode"}

            ``name`` is ``None`` for dialect-2 errors, where the JSON
            parse fails before the FC envelope's ``name`` field can be
            read.  ``reason`` is currently always ``"json_decode"`` —
            the only failure mode worth surfacing today; structurally-
            wrong FC envelopes (e.g. missing ``name``) stay silent
            because they look indistinguishable from prose-shaped JSON.
        """
        import re as _re
        calls: list[dict] = []
        errors: list[dict] = []

        def _walk_json(text: str, start: int) -> int:
            """Return index one past the matching `}` for the JSON object
            that starts at `text[start] == '{'`.  Honors string quoting +
            backslash escapes.  Returns -1 on unbalanced input."""
            if start >= len(text) or text[start] != '{':
                return -1
            depth = 0
            in_str = False
            esc = False
            for j in range(start, len(text)):
                ch = text[j]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == '\\':
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        return j + 1
            return -1

        # Dialect 1 — legacy `<tool>NAME</tool><args>{json}</args>` (and the
        # xLAM `[tool>...` / `[tool]...` bracket quirks on the open tag).
        # Anchor on the `<args>` prefix so the JSON walker picks up the full
        # object even with nested braces.
        anchor_legacy = _re.compile(r'[<\[]tool[>\]](\w+)</tool>\s*<args>\s*', _re.DOTALL)
        for m in anchor_legacy.finditer(text):
            name = m.group(1)
            i = m.end()
            end = _walk_json(text, i)
            if end < 0:
                continue
            args_str = text[i:end].strip()
            try:
                args = json.loads(args_str)
                calls.append({"tool": name, "args": args})
            except json.JSONDecodeError:
                logger.warning("Failed to parse tool args for %s: %s",
                               name, args_str[:100])
                errors.append({
                    "dialect": 1, "name": name, "reason": "json_decode",
                })

        # Dialect 2 — industry-standard `<tool_call>{"name": "X",
        # "arguments": {...}}</tool_call>`.  Walk the JSON directly from
        # right after the opening tag.  Whitespace / newlines between the
        # tag and the `{` are tolerated.
        anchor_std = _re.compile(r'<tool_call>\s*', _re.DOTALL)
        for m in anchor_std.finditer(text):
            i = m.end()
            end = _walk_json(text, i)
            if end < 0:
                continue
            body = text[i:end].strip()
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                logger.warning("Failed to parse <tool_call> body: %s",
                               body[:120])
                errors.append({
                    "dialect": 2, "name": None, "reason": "json_decode",
                })
                continue
            if not isinstance(parsed, dict):
                continue
            name = parsed.get("name")
            args = parsed.get("arguments", parsed.get("args", {}))
            if not isinstance(name, str) or not name:
                continue
            if not isinstance(args, dict):
                args = {}
            calls.append({"tool": name, "args": args})

        # Dialect 3 — bracketed-name (xLAM quirk, issue #82).
        # `[recall]{"query":"…"}</recall>` or `[datetime]GETCURRENTTIME()`.
        # The skill name IS the tag, so without a registry validation step
        # we'd false-positive on prose like `[note]` quoted in a chat
        # reply.  Skip the pass entirely if no tools are registered (the
        # validation gate would reject everything anyway).
        if self._tools:
            for m in _BRACKET_NAME_PRECHECK.finditer(text):
                name = m.group(1)
                if name not in self._tools:
                    continue
                i = m.end()
                # Skip whitespace immediately after the opening bracket.
                while i < len(text) and text[i] in " \t\n\r":
                    i += 1
                if i >= len(text):
                    continue

                args: Optional[dict] = None

                if text[i] == "{":
                    # Sub-form A: `[NAME]{json}</NAME>` — JSON args + close.
                    end = _walk_json(text, i)
                    if end < 0:
                        continue
                    try:
                        parsed_args = json.loads(text[i:end])
                    except json.JSONDecodeError:
                        logger.warning(
                            "Failed to parse bracket-name args for %s: %s",
                            name, text[i:end][:100],
                        )
                        errors.append({
                            "dialect": 3, "name": name, "reason": "json_decode",
                        })
                        continue
                    if not isinstance(parsed_args, dict):
                        continue
                    args = parsed_args
                    # The matching `</NAME>` close tag is *encouraged* but not
                    # required — xLAM emits it cleanly on most prompts but
                    # truncates on others.  We accept either way.
                else:
                    # Sub-form B: `[NAME]IDENT()` — empty args, function-name
                    # noise.  Bail if the noise spans more than ~50 chars
                    # (real-world cases like GETCURRENTDATEANDTIME stay
                    # well under that) or doesn't look like an identifier.
                    paren = text.find("()", i, i + 80)
                    if paren < 0:
                        continue
                    noise = text[i:paren].strip()
                    if not _BRACKET_NOISE_IDENT.match(noise):
                        continue
                    args = {}

                if args is not None:
                    calls.append({"tool": name, "args": args})

        return calls, errors

    def has_tool_call(self, text: str) -> bool:
        """Quick check if text contains a tool call marker.
        Accepts the three dialects the parser understands: the legacy
        `<tool>NAME</tool>` shape, the industry-standard
        `<tool_call>{...}</tool_call>` shape, and the bracketed-name
        xLAM quirk `[NAME]{json}</NAME>` / `[NAME]IDENT()` (gated on the
        name actually matching a registered tool, so prose like
        `[note]` in a chat reply doesn't fire).
        """
        has_legacy = (
            ("<tool>" in text or "[tool>" in text or "[tool]" in text or "<tool]" in text)
            and "</tool>" in text
        )
        has_std = "<tool_call>" in text and "</tool_call>" in text
        if has_legacy or has_std:
            return True

        if not self._tools:
            return False
        for m in _BRACKET_NAME_PRECHECK.finditer(text):
            if m.group(1) not in self._tools:
                continue
            # Peek past whitespace for the `{` (sub-form A) or
            # `<word>(` (sub-form B) that distinguishes a real call
            # from prose that happens to mention `[recall]`.  Without
            # this guard, `has_tool_call` over-fires on chat replies
            # whenever a tool name appears in brackets.
            j = m.end()
            while j < len(text) and text[j] in " \t\n\r":
                j += 1
            if j >= len(text):
                continue
            if text[j] == "{":
                return True
            # Sub-form B: identifier+`()` should appear within the same
            # window parse_tool_calls scans.
            if "()" in text[j:j + 80]:
                return True
        return False

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
