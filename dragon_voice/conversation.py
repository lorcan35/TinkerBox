"""Conversation engine for TinkerClaw.

Input-agnostic processing: receives text (from STT or keyboard),
loads context from MessageStore, sends to LLM, stores response.
Supports tool-calling and memory-augmented context.

refs #17, #18
"""

import logging
import re
import time
from typing import AsyncIterator, Optional

from dragon_voice.db import Database
from dragon_voice.messages import (
    MessageStore,
    CONTEXT_BUDGET_LOCAL,
    CONTEXT_BUDGET_CLOUD,
    trim_context_to_budget,
)
from dragon_voice.llm import create_llm, LLMBackend
from dragon_voice.config import LLMConfig

logger = logging.getLogger(__name__)

MAX_TOOL_CALLS = 3  # Prevent infinite tool-call loops

# Audit D5 fix: the ToolRegistry's tolerant parser accepts `<tool>`/`</tool>`
# + `<args>`/`</args>` with minor whitespace + stray closing chars. When we
# fall through to "no tool call (or max reached)" and yield the buffered
# response to the client, any raw tool XML in that text lands in the chat
# bubble. Strip both blocks before yielding so the user never sees markup.
_TOOL_MARKUP_RE = re.compile(
    r"<tool>[\s\S]*?</tool>\s*<args>[\s\S]*?</args>\s*>?", re.IGNORECASE
)


def _strip_tool_markup(text: str) -> str:
    """Remove `<tool>...</tool><args>...</args>` blocks from user-visible text."""
    return _TOOL_MARKUP_RE.sub("", text)


# Phase 2 H1 (issue #94): tool-call marker openers we recognize.  When
# any of these appears in the streaming buffer we hold back tokens from
# that position onward so we don't yield half-formed tool markup to the
# user.  See `_split_at_marker_boundary` for the rolling-detection
# semantics.
#
# Dialect coverage (must stay in sync with `tools/registry.py`):
#   1. Legacy + xLAM bracket quirks: `<tool>`, `[tool>`, `<tool]`, `[tool]`
#   2. Standard FC: `<tool_call>`
#   3. Bracketed-name (xLAM dialect 3): `[NAME]` — added at runtime
#      from the tool registry by `_split_at_marker_boundary`
_TOOL_MARKER_OPENERS_STATIC: tuple[str, ...] = (
    "<tool>",
    "[tool>",
    "<tool]",
    "[tool]",
    "<tool_call>",
)


def _split_at_marker_boundary(
    accumulated: str,
    registered_tool_names: set[str] | None = None,
) -> tuple[str, str]:
    """Split `accumulated` into (flushable, held) at the earliest tool-marker
    boundary so the caller can stream the flushable prefix while waiting
    for the held suffix to either complete into a parseable tool call or
    turn out to be benign prose.

    The boundary is the leftmost position of:
      * any complete known opener anywhere in the string, OR
      * a strict-prefix match of any known opener at the END of the string
        (i.e. an in-flight opener that hasn't completed yet)

    If neither condition fires, returns `(accumulated, "")` — everything
    is safe to flush right now.

    Notes
    -----
    * Stateless: callers re-invoke after each token append, passing the
      full accumulated buffer.  Cheap because the registered-tool set is
      small (~15 entries) and the openers are short.
    * Conservative on registered-tool dialect-3 partials: matches only on
      `[NAME]` (complete close-bracket) — we don't try to match a `[NAM`
      partial because false positives on prose like "[New York]" would
      stutter the stream visibly.  The complete-marker check still catches
      `[NAME]` once the close-bracket arrives.
    """
    # Two opener sets — they have different partial-matching semantics:
    #   * static openers participate in BOTH complete + tail-partial matching
    #   * dialect-3 `[NAME]` openers participate ONLY in complete matching
    #     because partial-matching them (e.g. holding back `[recal` on the
    #     way to `[recall]`) would also stutter on benign prose like
    #     `[Star Trek` or `[Mr. Smith` and visibly break streaming.
    static_openers: list[str] = list(_TOOL_MARKER_OPENERS_STATIC)
    dialect3_openers: list[str] = (
        [f"[{n}]" for n in registered_tool_names] if registered_tool_names else []
    )

    hold_at = len(accumulated)

    # 1) Earliest complete opener anywhere (both sets).
    for op in static_openers + dialect3_openers:
        idx = accumulated.find(op)
        if 0 <= idx < hold_at:
            hold_at = idx

    # 2) Tail partial — strict prefix of any STATIC opener at the end
    #    of the string.  Dialect-3 openers deliberately excluded; see
    #    note above.
    max_op_len = max((len(o) for o in static_openers), default=0)
    tail_window = accumulated[-max_op_len:] if max_op_len else ""
    for op in static_openers:
        for partial_len in range(1, len(op)):
            if tail_window.endswith(op[:partial_len]):
                tail_start = len(accumulated) - partial_len
                if tail_start < hold_at:
                    hold_at = tail_start
                break  # found longest partial for this opener; move on

    return accumulated[:hold_at], accumulated[hold_at:]


class ConversationEngine:
    """Processes text input through the LLM with persistent context.

    Input-agnostic: works for both voice (post-STT) and text (keyboard/API).
    Stores all messages in the MessageStore for history and resume.
    Supports tool-calling (ToolRegistry) and memory-augmented context (MemoryService).
    """

    def __init__(
        self,
        db: Database,
        message_store: MessageStore,
        llm_config: LLMConfig,
        tool_registry=None,
        memory_service=None,
    ) -> None:
        self._db = db
        self._messages = message_store
        self._llm_config = llm_config
        self._llm: Optional[LLMBackend] = None
        self._tool_registry = tool_registry
        self._memory_service = memory_service

    async def initialize(self) -> None:
        """Create and initialize the LLM backend."""
        self._llm = create_llm(self._llm_config)
        await self._llm.initialize()
        logger.info("ConversationEngine initialized with LLM: %s", self._llm.name)

    async def shutdown(self) -> None:
        """Shut down the LLM backend."""
        if self._llm:
            await self._llm.shutdown()
            self._llm = None

    @property
    def llm(self) -> Optional[LLMBackend]:
        """Access the underlying LLM backend (for pipeline integration)."""
        return self._llm

    async def process_text(
        self,
        session_id: str,
        text: str,
        input_mode: str = "text",
        audio_duration_s: Optional[float] = None,
    ) -> str:
        """Process text input with tool-calling and memory support (non-streaming).

        Returns the full response text after any tool calls are resolved.
        """
        if not self._llm:
            raise RuntimeError("ConversationEngine not initialized")

        # Store user message
        await self._messages.add_message(
            session_id=session_id,
            role="user",
            content=text,
            input_mode=input_mode,
            audio_duration_s=audio_duration_s,
        )

        await self._db.touch_session(session_id)

        # Build context with memory + tool descriptions
        context = await self._build_context(session_id, text)

        t0 = time.monotonic()
        tool_calls_made = 0

        while True:
            full_response = []
            async for token in self._llm.generate_stream_with_messages(context):
                full_response.append(token)

            response_text = "".join(full_response)

            # Check for tool calls
            if (self._tool_registry
                    and self._tool_registry.has_tool_call(response_text)
                    and tool_calls_made < MAX_TOOL_CALLS):

                tool_calls = self._tool_registry.parse_tool_calls(response_text)
                if tool_calls:
                    tool_call = tool_calls[0]
                    tool_calls_made += 1
                    logger.info("Tool call (sync): %s(%s)", tool_call["tool"], tool_call["args"])

                    # Wave 12 skill SDK: inject session_id so skills can
                    # resolve the per-Tab5 surface via
                    # SurfaceManager.surface_for(session_id, skill_id).
                    # Without this the skill pulls "unknown" and no
                    # widget ever reaches the user.  Existing tools that
                    # don't need session continue to ignore the key.
                    _args = dict(tool_call["args"] or {})
                    _args.setdefault("session_id", session_id)
                    result = await self._tool_registry.execute(tool_call["tool"], _args)

                    import json as _json
                    await self._messages.add_message(
                        session_id=session_id, role="assistant",
                        content=response_text, input_mode="system",
                        model=self._llm.name,
                    )
                    await self._messages.add_message(
                        session_id=session_id, role="tool",
                        content=f"<tool_result>{_json.dumps(result.get('result', result))}</tool_result>",
                        input_mode="system",
                    )
                    context = await self._messages.get_context(session_id)
                    continue

            break

        latency_ms = (time.monotonic() - t0) * 1000

        await self._messages.add_message(
            session_id=session_id,
            role="assistant",
            content=response_text,
            input_mode="system",
            model=self._llm.name,
            latency_ms=latency_ms,
        )

        logger.info(
            "Conversation turn (session=%s, latency=%.0fms, tools=%d): '%s' → '%s'",
            session_id, latency_ms, tool_calls_made, text[:50], response_text[:50],
        )
        return response_text

    async def _build_context(self, session_id: str, user_text: str) -> list[dict]:
        """Build LLM context with optional memory augmentation and tool descriptions.

        After assembling the full context (system prompt + memory + tools + history),
        trims oldest messages to fit within the model's token budget (US-P16).
        """
        # Mode-aware context depth: local models have tiny context windows,
        # cloud models (128K+) can use much more conversation history.
        is_local = self._llm_config.backend in ("ollama", "npu_genie", "lmstudio")
        max_msgs = 10 if is_local else 30
        context = await self._messages.get_context(session_id, max_messages=max_msgs)

        # Inject memory context before the user's message
        if self._memory_service:
            try:
                memory_ctx = await self._memory_service.get_relevant_context(user_text)
                if memory_ctx:
                    # Augment system prompt with memory context
                    if context and context[0]["role"] == "system":
                        context[0]["content"] += "\n\n" + memory_ctx
            except Exception as e:
                logger.warning("Memory context retrieval failed: %s", e)

        # Inject tool descriptions into system prompt
        # Use compact format for local models to save tokens
        if self._tool_registry:
            is_local = self._llm_config.backend in ("ollama", "npu_genie", "lmstudio")
            tool_desc = self._tool_registry.format_for_llm(compact=is_local)
            if tool_desc and context and context[0]["role"] == "system":
                context[0]["content"] += "\n" + tool_desc

        # ── Token budget enforcement (US-P16) ─────────────────────────
        # After all augmentation (memory, tools), trim oldest messages so
        # total context fits within the model's context window.
        budget = CONTEXT_BUDGET_LOCAL if is_local else CONTEXT_BUDGET_CLOUD
        context = trim_context_to_budget(context, budget)

        return context

    async def process_text_stream(
        self,
        session_id: str,
        text: str,
        input_mode: str = "text",
        audio_duration_s: Optional[float] = None,
        on_tool_call=None,
        on_tool_result=None,
        on_tool_error=None,
    ) -> AsyncIterator[str]:
        """Process text input with streaming response and tool-calling support.

        Args:
            session_id: Active session to converse in.
            text: The user's message.
            input_mode: 'voice' or 'text'.
            audio_duration_s: Duration of voice input (None for text).
            on_tool_call: Optional async callback(call_dict) for tool call events.
            on_tool_result: Optional async callback(result_dict) for tool result events.
            on_tool_error: Optional async callback(error_dict) — fires once
                per failed tool-call parse (γ2-M1, issue #104).  Pre-fix
                these failures were silent `logger.warning` lines; the
                WS handler wires this to a `tool_args_invalid` error
                frame so the user sees a transient toast instead of an
                empty/generic LLM reply.

        Yields:
            Text tokens as they arrive from the LLM.
        """
        if not self._llm:
            raise RuntimeError("ConversationEngine not initialized")

        # Store user message
        await self._messages.add_message(
            session_id=session_id,
            role="user",
            content=text,
            input_mode=input_mode,
            audio_duration_s=audio_duration_s,
        )

        # Touch session activity
        await self._db.touch_session(session_id)

        # Build context with memory + tool descriptions
        context = await self._build_context(session_id, text)

        t0 = time.monotonic()
        tool_calls_made = 0

        # Phase 2 H1 (issue #94): registered-tool name set is needed at
        # streaming time for dialect-3 ([NAME]{json}) marker detection.
        # Captured once per turn; live registry mutations between turns
        # are fine because we re-fetch each iteration's loop.
        tool_names: set[str] = (
            set(self._tool_registry._tools.keys())
            if self._tool_registry else set()
        )

        while True:
            # Stream LLM response
            full_response = []
            # Phase 2 H1 (issue #94): rolling buffer for marker-aware
            # streaming.  Pre-fix this loop suppressed every yield when
            # `tool_registry` was set, accumulated the entire LLM output
            # in `full_response`, and emitted a single yield at the
            # bottom — Tab5 stared at a silent caption for 60-90 s on
            # every tool-calling local turn.
            #
            # New behaviour: stream tokens immediately UNTIL a known
            # tool-marker opener appears in the rolling buffer.  Hold
            # back from the marker-start onward; once the LLM finishes
            # we either parse + execute a tool call (existing path) or
            # treat the held buffer as benign prose and flush it (with
            # `_strip_tool_markup` for safety).
            #
            # `_split_at_marker_boundary` is the rolling-detection
            # primitive — see its docstring for boundary rules.
            held = ""
            async for token in self._llm.generate_stream_with_messages(context):
                full_response.append(token)
                if not self._tool_registry:
                    # No registry → no tool detection needed; raw stream.
                    yield token
                    continue
                held += token
                flushable, held = _split_at_marker_boundary(held, tool_names)
                if flushable:
                    yield flushable

            response_text = "".join(full_response)

            # Check for tool calls
            if (self._tool_registry
                    and self._tool_registry.has_tool_call(response_text)
                    and tool_calls_made < MAX_TOOL_CALLS):

                # γ2-M1 (issue #104): use the error-surfacing variant so
                # malformed tool-call attempts emit a `tool_args_invalid`
                # WS frame instead of silently disappearing into a
                # logger.warning.  Errors are reported even when there's
                # at least one successful call in the same response —
                # the user wants to know "tool A worked, tool B was
                # skipped" rather than just seeing the partial result.
                tool_calls, tool_errors = (
                    self._tool_registry.parse_tool_calls_with_errors(response_text)
                )
                if tool_errors and on_tool_error:
                    for err in tool_errors:
                        try:
                            await on_tool_error(err)
                        except Exception as e:
                            logger.debug("on_tool_error callback error: %s", e)
                if tool_calls:
                    tool_call = tool_calls[0]  # Execute one at a time
                    tool_calls_made += 1
                    logger.info("Tool call detected: %s(%s)", tool_call["tool"], tool_call["args"])

                    # Notify caller (per-connection, not shared)
                    if on_tool_call:
                        try:
                            await on_tool_call(tool_call)
                        except Exception as e:
                            logger.debug("Callback error: %s", e)

                    # Execute the tool
                    # Wave 12 skill SDK: inject session_id so skills can
                    # resolve the per-Tab5 surface via
                    # SurfaceManager.surface_for(session_id, skill_id).
                    # Without this the skill pulls "unknown" and no
                    # widget ever reaches the user.  Existing tools that
                    # don't need session continue to ignore the key.
                    _args = dict(tool_call["args"] or {})
                    _args.setdefault("session_id", session_id)
                    result = await self._tool_registry.execute(tool_call["tool"], _args)

                    if on_tool_result:
                        try:
                            await on_tool_result(result)
                        except Exception as e:
                            logger.debug("Callback error: %s", e)

                    # Store tool interaction as messages
                    import json as _json
                    await self._messages.add_message(
                        session_id=session_id, role="assistant",
                        content=response_text, input_mode="system",
                        model=self._llm.name,
                    )
                    await self._messages.add_message(
                        session_id=session_id, role="tool",
                        content=f"<tool_result>{_json.dumps(result.get('result', result))}</tool_result>",
                        input_mode="system",
                    )

                    # Rebuild context with tool result and re-query LLM
                    context = await self._messages.get_context(session_id)
                    continue  # Loop back for next LLM call

            # No tool call (or max reached) — this is the final response.
            # Phase 2 H1 (issue #94): the streaming loop above has already
            # yielded everything up to the last marker-opener boundary.
            # `held` is the residual tail from the marker onward — usually
            # empty (no in-flight markers), or contains text that LOOKED
            # like a tool start but never completed (benign prose with
            # `<tool` literal, or malformed markup the parser rejected).
            #
            # Audit D5: still run `_strip_tool_markup` on the held tail so
            # if it DID contain a complete-but-unparseable
            # `<tool>X</tool><args>{}</args>` block (happens when the LLM
            # emitted markup after MAX_TOOL_CALLS was hit) the user
            # doesn't see raw XML.  Most turns this is a no-op.
            if self._tool_registry and held:
                cleaned = _strip_tool_markup(held)
                if cleaned:
                    yield cleaned

            break

        latency_ms = (time.monotonic() - t0) * 1000

        # Store final assistant response (only the LAST response, not duplicates)
        await self._messages.add_message(
            session_id=session_id,
            role="assistant",
            content=response_text,
            input_mode="system",
            model=self._llm.name,
            latency_ms=latency_ms,
        )

        logger.info(
            "Conversation streamed (session=%s, latency=%.0fms, tools=%d): '%s' → '%s'",
            session_id, latency_ms, tool_calls_made, text[:50], response_text[:50],
        )
