"""Tool-call parser — three-dialect XML/bracket marker recognition.

Wave 23 SOLID-audit follow-up — twenty-third sub-extract; first
slice from `tools/registry.py` (audit SRP-5: registry +
3-dialect parser + 2-format formatter all bundled in one
class).  The parser is a pure function (text in → call list
out) that doesn't need the registry's instance state EXCEPT to
validate dialect-3 names against the set of registered tools.

This module exposes the parser as free functions taking a
`registered_names` argument; `ToolRegistry` keeps thin
forwarding methods for backward compat with the 16+ existing
call sites.

## Three accepted dialects

  1. **LEGACY** (TinkerBox system-prompt format):
     ``<tool>NAME</tool><args>{"k": "v"}</args>``
     Dragon's own system prompt instructs models to emit this.

  2. **STANDARD** (industry-typical FC fine-tunes):
     ``<tool_call>{"name": "NAME", "arguments": {"k": "v"}}</tool_call>``
     Models trained for function-calling almost universally
     emit this regardless of system-prompt instruction — that's
     what they were fine-tuned on.

  3. **BRACKETED-NAME** (xLAM quirk, issue #82):
     ``[NAME]{"k": "v"}</NAME>`` — JSON args + matching close
     ``[NAME]UPPERCASE_IDENT()`` — empty args, function-call noise
     The skill name IS the tag.  REQUIRES the name to be in the
     registered tool set; without that gate the parser would
     fire on innocent prose like ``[note]`` quoted in a chat
     reply.

All three dialects are extracted into the same
``{"tool": name, "args": dict}`` record.

## Tolerance knobs (preserved verbatim from pre-extract)

  * Stray ``>`` after JSON, missing ``</args>``, extra
    whitespace all handled.
  * xLAM open-tag quirk on dialect 1: emits ``[tool>`` /
    ``<tool]`` / ``[tool]`` — the anchor regex accepts all
    four bracket combos because the closing ``</tool>`` is
    always intact.
  * Nested JSON values (e.g. ``{"filter": {"k": "v"}}``) —
    the brace-walker honours string quoting + backslash
    escapes so it won't truncate at the first inner ``}``.

## API

```python
calls, errors = parse_tool_calls_with_errors(
    text,
    registered_names=registry.names,
)

has = has_tool_call(text, registered_names=registry.names)
```

Both functions are pure — no side effects, no I/O.

## γ2-M1 (#104) closure preserved

`parse_tool_calls_with_errors` returns errors so the caller can
emit a `tool_parse_failed` event for user-visible agentic
failure (LLM emits a tool call, parser fails to load args, tool
never fires, user sees empty reply with no signal).  The
backward-compatible `parse_tool_calls` swallows errors for the
16 existing call sites that don't want them.
"""
from __future__ import annotations

import json
import logging
import re
from typing import AbstractSet, Optional, Tuple

logger = logging.getLogger(__name__)


# XML-style markers for tool calls in LLM output (legacy dialect).
#
# Tolerant regex: handles stray > after JSON, missing </args>, extra
# whitespace.  The `[<\[]tool[>\]]` open-tag class accepts `<tool>`,
# `[tool>`, `<tool]`, AND `[tool]` — quantized small models (notably
# xLAM) emit broken open brackets semi-randomly; the closing `</tool>`
# is always intact so there's no ambiguity.
TOOL_PATTERN = re.compile(
    r'[<\[]tool[>\]](\w+)</tool>\s*<args>\s*({.*?})\s*>?\s*</args>',
    re.DOTALL,
)
# Fallback: if </args> is missing entirely, grab JSON after <args>.
TOOL_PATTERN_LOOSE = re.compile(
    r'[<\[]tool[>\]](\w+)</tool>\s*<args>\s*({[^<]*})',
    re.DOTALL,
)

# Dialect-3 cheap pre-check.  Matches `[name]` where `name` looks like
# an identifier.  False positives at this stage are fine; the parser
# validates against the registered-tool set before accepting.
_BRACKET_NAME_PRECHECK = re.compile(r"\[([a-z_][a-z0-9_]*)\]")
# Dialect-3 noise-args sanity check — the GETCURRENTDATEANDTIME() form
# from xLAM should look like a function name (uppercase identifier),
# not prose.  3-50 chars keeps real-world cases without exploding the
# match window.
_BRACKET_NOISE_IDENT = re.compile(r"^[A-Z][A-Z0-9_]{2,49}$")


# Anchor regexes used by parse_tool_calls_with_errors.  Module-level
# so they compile once instead of per-call.
_ANCHOR_LEGACY = re.compile(
    r'[<\[]tool[>\]](\w+)</tool>\s*<args>\s*', re.DOTALL,
)
_ANCHOR_STD = re.compile(r'<tool_call>\s*', re.DOTALL)


def _walk_json(text: str, start: int) -> int:
    """Return index one past the matching `}` for the JSON object
    that starts at `text[start] == '{'`.  Honors string quoting +
    backslash escapes.  Returns -1 on unbalanced input.

    Used by the parser to handle nested JSON without truncating at
    the first inner `}` (audit P2 fix in pre-extract).
    """
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


def parse_tool_calls_with_errors(
    text: str,
    *,
    registered_names: AbstractSet[str],
) -> Tuple[list[dict], list[dict]]:
    """Parse tool-call markers from LLM output across all three
    dialects.  Returns (calls, errors) so the caller can decide
    whether to emit a `tool_parse_failed` event for γ2-M1 (#104)
    user-visible agentic failure.

    Args:
        text: LLM output text to scan.
        registered_names: Set of tool names known to the registry.
            Used to gate dialect 3 (bracketed-name) so the parser
            doesn't false-fire on prose like ``[note]`` quoted in
            a chat reply.

    Returns:
        ``(calls, errors)`` where:
        * ``calls`` is a list of ``{"tool": name, "args": dict}``.
        * ``errors`` is a list of ``{"dialect": int, "name": str
          | None, "reason": "json_decode"}`` records — one per
          marker that anchored cleanly but whose JSON body failed
          to parse.
    """
    calls: list[dict] = []
    errors: list[dict] = []

    # ── Dialect 1 — legacy <tool>NAME</tool><args>{json}</args> ──
    for m in _ANCHOR_LEGACY.finditer(text):
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
            logger.warning(
                "Failed to parse tool args for %s: %s",
                name, args_str[:100],
            )
            errors.append({
                "dialect": 1, "name": name, "reason": "json_decode",
            })

    # ── Dialect 2 — industry-standard <tool_call>{...}</tool_call> ──
    for m in _ANCHOR_STD.finditer(text):
        i = m.end()
        end = _walk_json(text, i)
        if end < 0:
            continue
        body = text[i:end].strip()
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            logger.warning(
                "Failed to parse <tool_call> body: %s", body[:120],
            )
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

    # ── Dialect 3 — bracketed-name (xLAM quirk, #82) ─────────────
    # Skip the pass entirely if no tools registered (the validation
    # gate would reject everything anyway).
    if registered_names:
        for m in _BRACKET_NAME_PRECHECK.finditer(text):
            name = m.group(1)
            if name not in registered_names:
                continue
            i = m.end()
            # Skip whitespace immediately after the opening bracket.
            while i < len(text) and text[i] in " \t\n\r":
                i += 1
            if i >= len(text):
                continue

            args: Optional[dict] = None

            if text[i] == "{":
                # Sub-form A: [NAME]{json}</NAME>
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
                        "dialect": 3, "name": name,
                        "reason": "json_decode",
                    })
                    continue
                if not isinstance(parsed_args, dict):
                    continue
                args = parsed_args
                # Matching </NAME> close is encouraged but not required —
                # xLAM emits it cleanly on most prompts but truncates
                # on others.  We accept either way.
            else:
                # Sub-form B: [NAME]IDENT() — empty args, fn-name noise.
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


def parse_tool_calls(
    text: str,
    *,
    registered_names: AbstractSet[str],
) -> list[dict]:
    """Backward-compatible wrapper around
    `parse_tool_calls_with_errors` that returns just the call list
    (no errors).  Preserves the original signature for the 16+
    existing call sites that don't surface parse errors.
    """
    calls, _errors = parse_tool_calls_with_errors(
        text, registered_names=registered_names,
    )
    return calls


def has_tool_call(
    text: str,
    *,
    registered_names: AbstractSet[str],
) -> bool:
    """Quick check if `text` contains a tool-call marker in any of
    the three accepted dialects.

    Same name-validation gate as the parser for dialect 3 — without
    it, prose like ``[note]`` in a chat reply would over-fire.

    Args:
        text: LLM output text to scan.
        registered_names: Set of tool names known to the registry.

    Returns:
        True iff at least one valid marker is present.
    """
    has_legacy = (
        ("<tool>" in text or "[tool>" in text
         or "[tool]" in text or "<tool]" in text)
        and "</tool>" in text
    )
    has_std = "<tool_call>" in text and "</tool_call>" in text
    if has_legacy or has_std:
        return True

    if not registered_names:
        return False
    for m in _BRACKET_NAME_PRECHECK.finditer(text):
        if m.group(1) not in registered_names:
            continue
        # Peek past whitespace for the `{` (sub-form A) or
        # `<word>(` (sub-form B) that distinguishes a real call
        # from prose that mentions `[recall]`.  Without this
        # guard, has_tool_call over-fires on chat replies whenever
        # a tool name appears in brackets.
        j = m.end()
        while j < len(text) and text[j] in " \t\n\r":
            j += 1
        if j >= len(text):
            continue
        if text[j] == "{":
            return True
        # Sub-form B: identifier+`()` should appear within the
        # same window parse_tool_calls scans.
        if "()" in text[j:j + 80]:
            return True
    return False
