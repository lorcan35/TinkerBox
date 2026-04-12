"""Message store and LLM context builder for TinkerClaw.

Append-only message persistence with context retrieval in OpenAI format.
All SQL goes through db.py — this module adds the context-building logic.

refs #17
"""

import logging
import secrets
from typing import Optional

from dragon_voice.db import Database

logger = logging.getLogger(__name__)

# Default system prompt if none is set on the session
DEFAULT_SYSTEM_PROMPT = (
    "You are Tinker, a helpful AI assistant on a portable device called "
    "TinkerClaw. Keep responses concise and conversational — they will "
    "be spoken aloud."
)


def _generate_message_id() -> str:
    """Generate a short hex message ID (12 hex chars)."""
    return secrets.token_hex(6)


class MessageStore:
    """Append-only message store with LLM context building.

    Messages are stored in the database and never mutated.
    Provides context retrieval in OpenAI chat format for the LLM.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        input_mode: str = "text",
        interrupted: bool = False,
        audio_duration_s: Optional[float] = None,
        token_count: Optional[int] = None,
        model: Optional[str] = None,
        latency_ms: Optional[float] = None,
    ) -> dict:
        """Store a message. Returns the message row as dict.

        Args:
            session_id: Which session this message belongs to.
            role: One of 'user', 'assistant', 'system', 'tool'.
            content: The message text content.
            input_mode: How the message entered: 'voice', 'text', or 'system'.
            interrupted: True if the user interrupted the assistant.
            audio_duration_s: Duration of voice input in seconds (None for text).
            token_count: LLM tokens used (None for user messages).
            model: Which LLM model generated this (None for user messages).
            latency_ms: End-to-end processing time in ms (None for user messages).
        """
        message_id = _generate_message_id()
        msg = await self._db.add_message(
            message_id=message_id,
            session_id=session_id,
            role=role,
            content=content,
            input_mode=input_mode,
            interrupted=interrupted,
            audio_duration_s=audio_duration_s,
            token_count=token_count,
            model=model,
            latency_ms=latency_ms,
        )
        logger.debug("Message stored: %s (session=%s, role=%s)", message_id, session_id, role)
        return msg

    async def get_messages(
        self,
        session_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Get raw message rows for a session (ascending by time)."""
        return await self._db.get_messages(session_id, limit=limit, offset=offset)

    async def get_context(
        self,
        session_id: str,
        max_messages: int = 20,
        system_prompt: Optional[str] = None,
    ) -> list[dict]:
        """Build an OpenAI-format message list for LLM context.

        Returns a list of dicts: [{role: "system"|"user"|"assistant", content: "..."}]

        The system prompt comes from (in priority order):
        1. The explicit system_prompt argument
        2. The session's system_prompt field
        3. DEFAULT_SYSTEM_PROMPT

        Args:
            session_id: Session to build context for.
            max_messages: Maximum number of conversation messages to include
                         (excluding the system prompt).
            system_prompt: Override system prompt (or None to use session/default).
        """
        # Determine system prompt
        if not system_prompt:
            session = await self._db.get_session(session_id)
            if session and session.get("system_prompt"):
                system_prompt = session["system_prompt"]
            else:
                system_prompt = DEFAULT_SYSTEM_PROMPT

        # Get the last N messages (we need to fetch from the end)
        # First count total, then offset to get the tail
        total = await self._db.count_messages(session_id)
        offset = max(0, total - max_messages)

        messages = await self._db.get_messages(session_id, limit=max_messages, offset=offset)

        # Build OpenAI format: system prompt + conversation messages
        context: list[dict] = [{"role": "system", "content": system_prompt}]

        for msg in messages:
            # Include user, assistant, and tool messages in LLM context
            # (tool results are needed for the LLM to see what tools returned)
            if msg["role"] in ("user", "assistant", "tool"):
                context.append({
                    "role": msg["role"],
                    "content": msg["content"],
                })

        return context

    async def count_messages(self, session_id: str) -> int:
        """Count total messages in a session."""
        return await self._db.count_messages(session_id)
