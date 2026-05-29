"""Async background smart-agent tier (Wave 3 of the 10x local program).

A heavy/agentic request ("research X and get back to me", "go through my unread
and draft replies") shouldn't block the voice loop for ~2 minutes. Instead the
fast tier acks instantly and a background task runs the SMART model
(Qwen3.5-4B on :1235) with the FULL tool set + a higher tool cap, then delivers
the result to the device via `deliver` (a channel_message the Tab5 notification
surface renders).

This is deliberately self-contained: it spins up its own LMStudioBackend pointed
at the smart server so it never contends with or mutates the live fast-path
ConversationEngine.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

from dragon_voice.config import LLMConfig
from dragon_voice.llm.lmstudio_llm import LMStudioBackend

logger = logging.getLogger(__name__)

SMART_URL = "http://127.0.0.1:1235/v1"
MAX_SMART_TOOL_CALLS = 5

# Smart-tier tool allowlist (~8). Kept TIGHT: Qwen3.5-4B on this CPU prefills
# slowly, and 15 schemas pushed a single step past the 300s read timeout
# (calls=[] content_len=0). 8 core tools keep a step well under the timeout
# while still covering research + the common calendar/email/tasks actions.
_SMART_TOOLS = {
    "web_search", "calendar_today", "calendar_create",
    "gmail_unread", "gmail_search", "gmail_send",
    "tasks_list", "tasks_add",
}

# Phrases that signal a heavy/deferred agentic task. Cheap heuristic — no extra
# LLM call. Tuned to be specific so normal single-shot commands ("add milk to my
# tasks", "what's the weather") stay on the fast tier.
_BG_TRIGGERS = (
    "get back to me", "and let me know", "research", "look into", "dig into",
    "go through my", "draft replies", "draft a reply to my", "take your time",
    "in the background", "when you get a chance", "when you have time",
    "figure out", "look through", "go over my", "summarize my",
)

SMART_SYS = (
    "You are Tinker's deep-thinking background agent. You have full access to the "
    "user's calendar, email, tasks, web search, and memory tools. Carry out the "
    "request thoroughly, using multiple tool calls as needed (read first, then "
    "act). When done, reply with a short, plain-language summary of what you did "
    "and what you found — no markdown, 1-3 sentences, suitable to be read aloud."
)


def is_background_request(text: str) -> bool:
    """True if the text looks like a heavy/deferred agentic task."""
    t = (text or "").lower()
    return any(k in t for k in _BG_TRIGGERS)


async def run_background_agent(
    text: str,
    registry,
    deliver: Callable[[str], Awaitable[None]],
) -> None:
    """Run the smart-tier agentic loop, then `await deliver(summary)`.

    Never raises — failures are reported through `deliver` so the user always
    hears back.
    """
    t0 = time.monotonic()
    cfg = LLMConfig(
        backend="lmstudio", local_backend="lmstudio",
        lmstudio_url=SMART_URL, lmstudio_model="default",
        native_tools=True, max_tokens=1024,
    )
    be = LMStudioBackend(cfg)
    final = ""
    tools_used = 0
    try:
        await be.initialize()
        tools = [
            t for t in registry.openai_tools()
            if t["function"]["name"] in _SMART_TOOLS
        ] or registry.openai_tools()
        messages = [
            {"role": "system", "content": SMART_SYS},
            {"role": "user", "content": text},
        ]
        for _step in range(MAX_SMART_TOOL_CALLS):
            res = await be.generate_with_tools(
                messages, tools, max_tokens=384, disable_thinking=True,
                timeout_s=900,  # async/background: latency-free, let Qwen finish
            )
            calls = res.get("tool_calls") or []
            logger.info(
                "bg-agent step %d: calls=%s content_len=%d",
                _step, [c.get("name") for c in calls],
                len(res.get("content") or ""),
            )
            if not calls:
                final = (res.get("content") or "").strip()
                break
            call = calls[0]
            name = call["name"]
            args = dict(call.get("args") or {})
            tools_used += 1
            logger.info("bg-agent tool: %s(%s)", name, args)
            messages.append({
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }],
            })
            result = await registry.execute(name, args)
            messages.append({
                "role": "tool", "tool_call_id": "c",
                "content": json.dumps(result.get("result", result))[:1500],
            })
        if not final:
            # Used all tool steps without writing an answer — force a final
            # no-tool synthesis from what was gathered.
            messages.append({
                "role": "user",
                "content": ("Now stop searching and answer my original request "
                            "in 1-3 short, plain sentences suitable to be read "
                            "aloud. Do not call any tool."),
            })
            try:
                res = await be.generate_with_tools(
                    messages, [], max_tokens=400, disable_thinking=True,
                    timeout_s=900,
                )
                final = (res.get("content") or "").strip()
            except Exception:  # noqa: BLE001
                logger.exception("bg-agent synthesis failed")
            if not final:
                final = "I gathered the info but couldn't summarize it in time."
    except Exception as e:  # noqa: BLE001 — must always report back
        logger.exception("bg-agent failed")
        final = f"Sorry, that background task hit an error: {e}"
    finally:
        try:
            await be.shutdown()
        except Exception:  # noqa: BLE001
            pass
    logger.info(
        "bg-agent done (%.0fs, tools=%d): '%s' → '%s'",
        time.monotonic() - t0, tools_used, text[:50], final[:80],
    )
    try:
        await deliver(final)
    except Exception:  # noqa: BLE001
        logger.exception("bg-agent deliver failed")


_bg_tasks: set = set()


def spawn_background_agent(text, registry, deliver) -> None:
    """Fire-and-forget the background agent (holds a ref so it isn't GC'd)."""
    task = asyncio.create_task(run_background_agent(text, registry, deliver))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
