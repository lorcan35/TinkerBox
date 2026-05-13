"""W7-C: mirror gateway-side `remember` tool calls into Dragon's local memory.

In voice mode 3 the LLM lives on the OpenClaw gateway agent and runs its own
tools — Dragon doesn't see the tool *execution*, only the SSE-streamed
`delta.tool_calls` events.  When the gateway agent calls ``remember`` to
store a user fact, that fact lands in the *gateway's* private agent memory
(workspace files), not in Dragon's ``memory_facts`` table.

The "memory bridge" the W7-C audit slot called for can't route to a
``gateway.memory.*`` RPC because OpenClaw's gateway has no such verb (see
``openclaw/src/gateway/server-methods-list.ts``).  What we *can* do is
mirror the user-facing intent: when the gateway emits a ``remember`` tool
call, also store the same fact locally with ``source="gateway"`` so:

  * the dashboard / Tab5 memory browser sees what the user told the
    gateway agent
  * future Dragon turns (e.g. if the user switches back to mode 0/1/2)
    still have the fact in the augmented-context prompt
  * the cross-stack provenance is explicit — mode-3 user statements are
    bucketed separately from Dragon-conversation-derived memories

The mirror is best-effort: any failure (memory service missing, embedding
backend down, sqlite locked) is logged and swallowed.  Mode-3 LLM streaming
must NEVER tear down because of a memory-mirror hiccup.

Note: ``recall`` is intentionally *not* mirrored.  The gateway agent's
recall queries the gateway's local memory; Dragon's memory may have
different content and surfacing it would confuse the agent's reasoning.
The agent_log W7-A.3 source-bucket already captures the recall *event*
for observability; that's enough.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


async def mirror_gateway_tool_call(
    tool: str,
    args: Any,
    memory_service: Optional[Any],
    session_id: str = "",
) -> bool:
    """If `tool == "remember"` and a fact is present, store it locally with
    ``source="gateway"``.

    Returns:
        True if a fact was mirrored, False if the call was skipped (not
        remember, missing fact, no memory service, or error).  Callers
        should ignore the return value in the hot path — it exists for
        tests.
    """
    if memory_service is None:
        # Memory subsystem disabled or not yet initialised.  Don't warn —
        # this is expected during early boot before run_startup finishes.
        return False

    if tool != "remember":
        # Only remember mirrors today.  Recall stays gateway-local for the
        # reasons noted in the module docstring.
        return False

    fact: str = ""
    if isinstance(args, dict):
        # The remember tool's canonical arg is `fact`.  Some FC-trained
        # models emit `content` or `text` instead; accept all three so
        # we don't lose user intent to gateway-side variation.
        for key in ("fact", "content", "text"):
            v = args.get(key)
            if isinstance(v, str) and v.strip():
                fact = v.strip()
                break

    if not fact:
        return False

    try:
        await memory_service.store_fact(
            fact, source="gateway", session_id=session_id or None,
        )
    except Exception as e:  # noqa: BLE001 — best-effort, swallow + log
        logger.warning(
            "W7-C: gateway 'remember' mirror failed (fact=%.60s): %s",
            fact, e,
        )
        return False

    logger.info(
        "W7-C: mirrored gateway 'remember' into local memory_facts "
        "(source=gateway, session=%s, fact=%.60s)",
        session_id or "<none>", fact,
    )
    return True
