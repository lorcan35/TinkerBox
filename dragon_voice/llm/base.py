"""Abstract base class for LLM backends."""

from abc import ABC, abstractmethod
from typing import AsyncIterator


class LLMBackend(ABC):
    """Interface that all LLM backends must implement."""

    @abstractmethod
    async def initialize(self) -> None:
        """Verify connectivity and prepare for inference. Called once at startup."""
        ...

    @abstractmethod
    async def generate_stream(
        self, prompt: str, system_prompt: str = ""
    ) -> AsyncIterator[str]:
        """Stream text tokens from the LLM.

        Args:
            prompt: The user's message / transcribed speech.
            system_prompt: System prompt for the conversation. If empty,
                          the backend should use its configured default.

        Yields:
            Text tokens (strings) as they arrive from the model.
        """
        ...

    async def generate_stream_with_messages(
        self, messages: list[dict]
    ) -> AsyncIterator[str]:
        """Stream tokens using a full OpenAI-format message list as context.

        Default implementation formats messages into a single prompt and
        delegates to generate_stream(). Backends that support native multi-turn
        (e.g. Ollama /api/chat) should override this.

        Args:
            messages: List of {role: "system"|"user"|"assistant", content: "..."}.
                     The last message is the current user input.
        """
        # Extract system prompt and current user message
        system_prompt = ""
        current_prompt = ""
        history = []

        for msg in messages:
            if msg["role"] == "system":
                system_prompt = msg["content"]
            elif msg == messages[-1] and msg["role"] == "user":
                current_prompt = msg["content"]
            else:
                history.append(msg)

        if not current_prompt:
            # Fallback: use last message content regardless of role
            current_prompt = messages[-1]["content"] if messages else ""

        # Build a formatted prompt with history context
        parts = []
        if system_prompt:
            parts.append(f"System: {system_prompt}\n")
        for msg in history[-6:]:  # Last 3 turns
            role = "User" if msg["role"] == "user" else "Assistant"
            parts.append(f"{role}: {msg['content']}")
        parts.append(f"User: {current_prompt}")
        parts.append("Assistant:")

        formatted = "\n".join(parts)

        async for token in self.generate_stream(formatted, ""):
            yield token

    @abstractmethod
    async def shutdown(self) -> None:
        """Release resources. Called once at server shutdown."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable backend name for logging and status pages."""
        ...
