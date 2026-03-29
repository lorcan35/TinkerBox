"""Conversation engine for TinkerClaw.

Input-agnostic processing: receives text (from STT or keyboard),
loads context from MessageStore, sends to LLM, stores response.
The caller decides whether to TTS the result.

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


class ConversationEngine:
    """Processes text input through the LLM with persistent context.

    Input-agnostic: works for both voice (post-STT) and text (keyboard/API).
    Stores all messages in the MessageStore for history and resume.
    """

    def __init__(
        self,
        db: Database,
        message_store: MessageStore,
        llm_config: LLMConfig,
    ) -> None:
        self._db = db
        self._messages = message_store
        self._llm_config = llm_config
        self._llm: Optional[LLMBackend] = None

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
        """Process a text input through the conversation engine.

        Stores the user message, gets LLM response, stores the response.
        Returns the full response text.

        Args:
            session_id: Active session to converse in.
            text: The user's message (from STT transcription or text input).
            input_mode: 'voice' or 'text' — how the input arrived.
            audio_duration_s: Duration of voice input (None for text).

        Returns:
            The assistant's full response text.
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

        # Get system prompt from session
        session = await self._db.get_session(session_id)
        system_prompt = ""
        if session and session.get("system_prompt"):
            system_prompt = session["system_prompt"]

        # Generate LLM response (streaming, collect full text)
        t0 = time.monotonic()
        full_response = []

        async for token in self._llm.generate_stream(text, system_prompt):
            full_response.append(token)

        response_text = "".join(full_response)
        latency_ms = (time.monotonic() - t0) * 1000

        # Store assistant response
        await self._messages.add_message(
            session_id=session_id,
            role="assistant",
            content=response_text,
            input_mode="system",
            model=self._llm.name,
            latency_ms=latency_ms,
        )

        logger.info(
            "Conversation turn (session=%s, latency=%.0fms): '%s' → '%s'",
            session_id, latency_ms, text[:50], response_text[:50],
        )
        return response_text

    async def process_text_stream(
        self,
        session_id: str,
        text: str,
        input_mode: str = "text",
        audio_duration_s: Optional[float] = None,
    ) -> AsyncIterator[str]:
        """Process text input with streaming response.

        Same as process_text but yields tokens as they arrive from the LLM.
        The full response is stored in the MessageStore after streaming completes.

        Args:
            session_id: Active session to converse in.
            text: The user's message.
            input_mode: 'voice' or 'text'.
            audio_duration_s: Duration of voice input (None for text).

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

        # Get system prompt from session
        session = await self._db.get_session(session_id)
        system_prompt = ""
        if session and session.get("system_prompt"):
            system_prompt = session["system_prompt"]

        # Stream LLM response, yielding tokens and collecting full text
        t0 = time.monotonic()
        full_response = []

        async for token in self._llm.generate_stream(text, system_prompt):
            full_response.append(token)
            yield token

        response_text = "".join(full_response)
        latency_ms = (time.monotonic() - t0) * 1000

        # Store assistant response after streaming completes
        await self._messages.add_message(
            session_id=session_id,
            role="assistant",
            content=response_text,
            input_mode="system",
            model=self._llm.name,
            latency_ms=latency_ms,
        )

        logger.info(
            "Conversation streamed (session=%s, latency=%.0fms): '%s' → '%s'",
            session_id, latency_ms, text[:50], response_text[:50],
        )
