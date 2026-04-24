"""Tool-response natural-language wrap synthesis.

Purpose
-------
Some function-calling-fine-tuned LLMs (Salesforce/xLAM, distil-labs/
functiongemma, LiquidAI/LFM2.5-FC, …) emit a tool call and then stop,
leaving no conversational text for the user.  ConversationEngine
faithfully strips the tool-call markup from the user-visible text
stream — net result is an empty chat bubble even when the tool itself
fired correctly and did real work.

This module contains a small per-tool template library that synthesises
a one-line natural-language acknowledgement from the *tool result*
(not from another LLM inference — the whole point is to be fast +
deterministic + free).  Dragon's server wires this up so it runs only
when:

  (a) at least one tool fired during the turn, AND
  (b) the LLM's own final-text output was empty or stripped to empty.

Happy-path turns where the LLM wrote a real reply get no wrap — the
natural-language text the model produced is always preferred.

Design constraints
------------------
* Zero LLM calls.  All wrap synthesis is templated Python string ops.
* Never block.  Each template is a pure function taking a dict.
* Never overwrite the model's reply.  Only fills an empty one.
* Stay short.  One sentence.  Match Dragon's "concise assistant" tone.
* Degrade gracefully.  If a tool isn't in the registry, return a
  generic "done" ack rather than raise.

See docs/AUDIT.md "Local-mode gauntlet Round 2 + 3" for the observed
empty-bubble pattern that motivates this module.  Refs #75.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

# Each wrap function takes the tool result dict (same shape as
# ToolRegistry.execute returns — {"tool": name, "result": ..., "execution_ms": N})
# and returns a user-facing one-liner, or None to signal "no good
# template for this result, fall through to the generic wrap."
Wrap = Callable[[dict], Optional[str]]


def _result_of(call: dict) -> Any:
    """Extract the tool's `result` payload regardless of whether it came
    in as the full execute() envelope or a raw value."""
    if isinstance(call, dict) and "result" in call:
        return call["result"]
    return call


def _wrap_datetime(call: dict) -> Optional[str]:
    r = _result_of(call)
    if isinstance(r, dict):
        time_str = r.get("time") or r.get("datetime") or r.get("now")
        tz = r.get("timezone") or r.get("tz")
        if time_str and tz:
            return f"It's {time_str} ({tz})."
        if time_str:
            return f"It's {time_str}."
    if isinstance(r, str) and r:
        return f"It's {r}."
    return None


def _wrap_calculator(call: dict) -> Optional[str]:
    r = _result_of(call)
    if isinstance(r, dict):
        val = r.get("result") if "result" in r else r.get("value")
        expr = r.get("expression") or r.get("input")
        if val is not None and expr:
            return f"{expr} = {val}."
        if val is not None:
            return f"That's {val}."
    if isinstance(r, (int, float, str)) and r != "":
        return f"That's {r}."
    return None


def _wrap_unit_converter(call: dict) -> Optional[str]:
    r = _result_of(call)
    if isinstance(r, dict):
        val = r.get("value") or r.get("result")
        unit = r.get("unit") or r.get("to_unit")
        if val is not None and unit:
            return f"That's {val} {unit}."
        if val is not None:
            return f"That's {val}."
    return None


def _wrap_weather(call: dict) -> Optional[str]:
    r = _result_of(call)
    if isinstance(r, dict):
        loc = r.get("location") or r.get("city") or "there"
        temp = r.get("temperature") or r.get("temp")
        summary = r.get("summary") or r.get("conditions") or r.get("description")
        if temp is not None and summary:
            return f"In {loc}: {summary}, {temp}°."
        if summary:
            return f"In {loc}: {summary}."
        if temp is not None:
            return f"It's {temp}° in {loc}."
    return None


def _wrap_remember(call: dict) -> Optional[str]:
    """Used by both `remember` and `store_fact` tool names."""
    # Try to pull the stored fact from args first (the call's input),
    # then from result.  Clients pass the call record {tool, args, result…}.
    fact = None
    if isinstance(call, dict):
        args = call.get("args") or {}
        if isinstance(args, dict):
            fact = args.get("fact") or args.get("content") or args.get("text")
    r = _result_of(call)
    if not fact and isinstance(r, dict):
        fact = r.get("stored") or r.get("fact")
    if fact:
        # Trim trailing period/punctuation to avoid ".." in output.
        clean = str(fact).rstrip(".!?,;:").strip()
        return f"Got it — {clean}."
    return "Got it — noted."


def _wrap_recall(call: dict) -> Optional[str]:
    r = _result_of(call)
    if isinstance(r, dict):
        # recall_facts returns a list of facts under "results" or "facts"
        items = r.get("results") or r.get("facts") or r.get("hits") or []
        if isinstance(items, list) and items:
            first = items[0]
            text = first.get("content") if isinstance(first, dict) else str(first)
            if text:
                if len(items) == 1:
                    return str(text).strip().rstrip(".") + "."
                return f"{str(text).strip().rstrip('.')}. (+{len(items)-1} more.)"
        return "I don't have anything stored about that yet."
    return None


def _wrap_forget(call: dict) -> Optional[str]:
    return "OK — forgotten."


def _wrap_web_search(call: dict) -> Optional[str]:
    r = _result_of(call)
    if isinstance(r, dict):
        items = r.get("results") or r.get("hits") or []
        if isinstance(items, list) and items:
            first = items[0]
            title = first.get("title") if isinstance(first, dict) else str(first)
            if title:
                return f"Top hit: {title}."
        return "I searched but didn't find a good match."
    return None


def _wrap_system_info(call: dict) -> Optional[str]:
    r = _result_of(call)
    if isinstance(r, dict):
        # Hand the user a short human-readable summary of whatever
        # system_info returned (ram/cpu/disk keys commonly present).
        bits: list[str] = []
        for key in ("ram", "cpu", "disk", "uptime"):
            v = r.get(key)
            if isinstance(v, (str, int, float)) and v != "":
                bits.append(f"{key}: {v}")
        if bits:
            return "Dragon: " + ", ".join(bits) + "."
    return None


def _wrap_stock_ticker(call: dict) -> Optional[str]:
    r = _result_of(call)
    if isinstance(r, dict):
        sym = r.get("symbol") or r.get("ticker")
        price = r.get("price") or r.get("last")
        if sym and price is not None:
            return f"{sym} is at {price}."
    return None


def _wrap_timer(call: dict) -> Optional[str]:
    """Plain timer (not the widget-emitting `timesense`)."""
    r = _result_of(call)
    duration: Any = None
    if isinstance(r, dict):
        duration = r.get("duration_s") or r.get("seconds") or r.get("duration")
    if duration is None and isinstance(call, dict):
        args = call.get("args") or {}
        if isinstance(args, dict):
            duration = args.get("duration_s") or args.get("seconds") or args.get("duration")
    if duration is not None:
        return f"Timer running for {duration} seconds."
    return "Timer started."


def _wrap_timesense(call: dict) -> Optional[str]:
    """Widget-emitting timer.  Live updates come via `widget_live`; the
    wrap only needs to acknowledge the setup."""
    r = _result_of(call)
    duration: Any = None
    if isinstance(r, dict):
        duration = r.get("duration_s") or r.get("seconds") or r.get("duration")
    if duration is None and isinstance(call, dict):
        args = call.get("args") or {}
        if isinstance(args, dict):
            duration = args.get("duration_s") or args.get("seconds") or args.get("duration")
    if duration is not None:
        return f"Timer set for {duration} seconds."
    return "Timer set."


def _wrap_quick_poll(call: dict) -> Optional[str]:
    """Poll widget acks via the widget_prompt ack, so the wrap here
    just nudges the user to tap."""
    return "Tap one."


def _wrap_note(call: dict) -> Optional[str]:
    r = _result_of(call)
    if isinstance(r, dict):
        title = r.get("title") or r.get("name")
        if title:
            return f"Saved note: {title}."
    return "Noted."


# Tool-name → wrap function.  Dragon ships with a handful of tools
# under two names each (e.g. `remember` and `store_fact` both hit the
# same store-fact tool) so we register aliases explicitly rather than
# trying to guess.  Unlisted tools fall through to `generic_wrap`.
_WRAPS: dict[str, Wrap] = {
    "datetime": _wrap_datetime,
    "calculator": _wrap_calculator,
    "unit_converter": _wrap_unit_converter,
    "weather": _wrap_weather,
    "remember": _wrap_remember,
    "store_fact": _wrap_remember,
    "recall": _wrap_recall,
    "recall_facts": _wrap_recall,
    "forget": _wrap_forget,
    "forget_fact": _wrap_forget,
    "web_search": _wrap_web_search,
    "system_info": _wrap_system_info,
    "stock_ticker": _wrap_stock_ticker,
    "timer": _wrap_timer,
    "timesense": _wrap_timesense,
    "quick_poll": _wrap_quick_poll,
    "note": _wrap_note,
}


def generic_wrap(calls: list[dict]) -> str:
    """Fallback when either no per-tool template matches or the tool is
    not in our table.  Keeps the user informed that Dragon did
    *something* on their behalf without pretending it was meaningful."""
    if not calls:
        return "Done."
    names = []
    for c in calls:
        if isinstance(c, dict):
            n = c.get("tool") or c.get("name")
            if n:
                names.append(str(n))
    if not names:
        return "Done."
    if len(names) == 1:
        return f"OK — ran {names[0]}."
    unique = list(dict.fromkeys(names))
    return "OK — ran " + ", ".join(unique) + "."


def synthesize_wrap(calls: list[dict]) -> str:
    """Public entrypoint.  Takes the list of tool-call records collected
    during the turn (each a dict with at least `tool` / `args` / `result`)
    and returns a single sentence the server can send to the user as
    the assistant's reply.

    Selection rules:
      * If exactly one tool fired AND the per-tool template yielded a
        non-empty string → return that.
      * If multiple tools fired → join up to three per-tool snippets
        with spaces.  Dedup identical ones.
      * If a tool has no template or its template returned None →
        include it in the generic fallback instead.
      * If `calls` is empty (defensive) → return "Done." (the caller
        should normally not invoke us on an empty list).

    Never raises.  Always returns a non-empty string.
    """
    if not calls:
        return "Done."

    snippets: list[str] = []
    untemplated: list[dict] = []

    for call in calls:
        if not isinstance(call, dict):
            continue
        name = call.get("tool") or call.get("name")
        wrap = _WRAPS.get(str(name or ""))
        if wrap is None:
            untemplated.append(call)
            continue
        try:
            snippet = wrap(call)
        except Exception:
            snippet = None
        if snippet:
            snippets.append(snippet)
        else:
            untemplated.append(call)

    # Dedup in order — repeated identical snippets read badly
    # (xLAM sometimes fires the same tool twice in a loop).
    seen: set[str] = set()
    unique_snippets: list[str] = []
    for s in snippets:
        if s not in seen:
            unique_snippets.append(s)
            seen.add(s)

    if untemplated:
        unique_snippets.append(generic_wrap(untemplated))

    if not unique_snippets:
        return generic_wrap(calls)

    # Cap at 3 snippets to keep replies readable.
    if len(unique_snippets) > 3:
        unique_snippets = unique_snippets[:3] + [f"(+{len(unique_snippets)-3} more)"]

    return " ".join(unique_snippets)
