"""Message store and LLM context builder for TinkerClaw.

Append-only message persistence with context retrieval in OpenAI format.
All SQL goes through db.py — this module adds the context-building logic.

refs #17
"""

import base64
import json
import logging
import os
import secrets
from typing import Any, Optional

from dragon_voice.db import Database

logger = logging.getLogger(__name__)


# ── Multimodal content marker (#183 PR 3) ─────────────────────────
# Multimodal user messages (image + text) are persisted as JSON in the
# `content` text column with a recognisable marker so we can hydrate
# them back to OpenAI multimodal format on context build, and so we
# never confuse a plain text message that happens to start with `{`
# with a multimodal payload.
_MM_MARKER = "__mm__:"


def encode_multimodal_content(media_id: str, text: str) -> str:
    """Encode a multimodal user message for storage in `messages.content`.

    Stores the media_id reference (not the raw base64) to keep the DB
    small. Hydration happens at context-build time via
    `_hydrate_multimodal_content` which reads the file off disk and
    inlines it as a data URI.
    """
    payload = {"media_id": media_id, "text": text}
    return _MM_MARKER + json.dumps(payload, separators=(",", ":"))


def is_multimodal_content(content: str) -> bool:
    return isinstance(content, str) and content.startswith(_MM_MARKER)


def _decode_multimodal_marker(content: str) -> Optional[dict]:
    if not is_multimodal_content(content):
        return None
    try:
        return json.loads(content[len(_MM_MARKER):])
    except json.JSONDecodeError:
        logger.warning("Malformed multimodal marker in stored content")
        return None


# ── Token budget helpers ──────────────────────────────────────────────

# Context window sizes (tokens) by backend class
CONTEXT_BUDGET_LOCAL = 25_600    # 80% of 32K (qwen3:1.7b, ollama, npu_genie)
CONTEXT_BUDGET_CLOUD = 100_000  # ~80% of 128K (Claude/GPT via OpenRouter)


def estimate_tokens(text: str) -> int:
    """Rough token count estimate: ~4 chars per token."""
    return max(1, len(text) // 4)


def trim_context_to_budget(messages: list[dict], budget_tokens: int) -> list[dict]:
    """Remove oldest non-system messages until context fits within budget.

    Preserves:
      - messages[0] (system prompt) — always kept
      - messages[-1] (latest user message) — always kept

    Removes from index 1 onward (oldest conversation messages first).
    """
    if len(messages) <= 2:
        return messages

    total = sum(estimate_tokens(m.get("content", "")) for m in messages)
    removed_count = 0
    while total > budget_tokens and len(messages) > 2:
        removed = messages.pop(1)  # remove oldest after system prompt
        total -= estimate_tokens(removed.get("content", ""))
        removed_count += 1

    if removed_count > 0:
        logger.info(
            "Trimmed %d messages to fit token budget (%d tokens, budget=%d)",
            removed_count, total, budget_tokens,
        )

    return messages

# Default system prompt if none is set on the session
DEFAULT_SYSTEM_PROMPT = (
    "You are Tinker, a helpful AI assistant on a portable device called "
    "TinkerClaw. Keep responses concise and conversational — they will "
    "be spoken aloud."
)


def _generate_message_id() -> str:
    """Generate a short hex message ID (12 hex chars)."""
    return secrets.token_hex(6)


async def _hydrate_multimodal_content(content: str, media_store: Any) -> Any:
    """Convert a stored multimodal-marker string to OpenAI multimodal content.

    Returns the original string unchanged when:
      - Content isn't multimodal-marked (the common case — text turns)
      - media_store is None (caller didn't opt in to hydration)
      - The referenced media file is gone (TTL expired or deleted)

    On expiry, returns a placeholder text so the LLM doesn't hallucinate
    against a missing image.
    """
    decoded = _decode_multimodal_marker(content)
    if decoded is None:
        return content
    media_id = decoded.get("media_id", "")
    text = decoded.get("text", "")
    if media_store is None or not media_id:
        return f"{text} [image attached: {media_id}]"
    try:
        path = await media_store.get_path(media_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("media_store.get_path(%s) failed: %s", media_id, e)
        return f"{text} [image expired]"
    if not path or not os.path.exists(path):
        return f"{text} [image expired]"
    # Read + base64 inline.  Done sync because we're inside an async
    # context-build that's not on the request hot path; the file is
    # already cached by MediaStore so this is fast.
    try:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
    except OSError as e:
        logger.warning("media file read failed (%s): %s", path, e)
        return f"{text} [image expired]"
    return [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        {"type": "text", "text": text},
    ]


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
        media_id: Optional[str] = None,
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
            media_id: When present, persist as a multimodal message —
                content gets encoded with the multimodal marker so
                `get_context` can hydrate it back into OpenAI multimodal
                format.  Used by `_handle_user_media` (#183).
        """
        message_id = _generate_message_id()
        if media_id:
            content = encode_multimodal_content(media_id, content)
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
        media_store: Optional[Any] = None,
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
                content = await _hydrate_multimodal_content(
                    msg["content"], media_store
                )
                context.append({
                    "role": msg["role"],
                    "content": content,
                })

        return context

    async def count_messages(self, session_id: str) -> int:
        """Count total messages in a session."""
        return await self._db.count_messages(session_id)
