"""Conversation engine for TinkerClaw.

Input-agnostic processing: receives text (from STT or keyboard),
loads context from MessageStore, sends to LLM, stores response.
Supports tool-calling and memory-augmented context.

refs #17, #18
"""

import logging
import time
from typing import AsyncIterator, Optional

from dragon_voice.db import Database
from dragon_voice.messages import MessageStore
from dragon_voice.llm import create_llm, LLMBackend
from dragon_voice.config import LLMConfig

logger = logging.getLogger(__name__)

MAX_TOOL_CALLS = 3  # Prevent infinite tool-call loops


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

                    result = await self._tool_registry.execute(tool_call["tool"], tool_call["args"])

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
        """Build LLM context with optional memory augmentation and tool descriptions."""
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

        return context

    async def process_text_stream(
        self,
        session_id: str,
        text: str,
        input_mode: str = "text",
        audio_duration_s: Optional[float] = None,
        on_tool_call=None,
        on_tool_result=None,
    ) -> AsyncIterator[str]:
        """Process text input with streaming response and tool-calling support.

        Args:
            session_id: Active session to converse in.
            text: The user's message.
            input_mode: 'voice' or 'text'.
            audio_duration_s: Duration of voice input (None for text).
            on_tool_call: Optional async callback(call_dict) for tool call events.
            on_tool_result: Optional async callback(result_dict) for tool result events.

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

        while True:
            # Stream LLM response
            full_response = []
            async for token in self._llm.generate_stream_with_messages(context):
                full_response.append(token)
                # Don't yield tokens if this might be a tool call (buffer first)
                if not self._tool_registry:
                    yield token

            response_text = "".join(full_response)

            # Check for tool calls
            if (self._tool_registry
                    and self._tool_registry.has_tool_call(response_text)
                    and tool_calls_made < MAX_TOOL_CALLS):

                tool_calls = self._tool_registry.parse_tool_calls(response_text)
                if tool_calls:
                    tool_call = tool_calls[0]  # Execute one at a time
                    tool_calls_made += 1
                    logger.info("Tool call detected: %s(%s)", tool_call["tool"], tool_call["args"])

                    # Notify caller (per-connection, not shared)
                    if on_tool_call:
                        try:
                            await on_tool_call(tool_call)
                        except Exception:
                            pass

                    # Execute the tool
                    result = await self._tool_registry.execute(tool_call["tool"], tool_call["args"])

                    if on_tool_result:
                        try:
                            await on_tool_result(result)
                        except Exception:
                            pass

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

            # No tool call (or max reached) — this is the final response
            if self._tool_registry:
                for token in full_response:
                    yield token

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
