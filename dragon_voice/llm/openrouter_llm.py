"""OpenRouter LLM backend.

Uses the OpenRouter API (OpenAI-compatible) for cloud-based LLM inference.
Supports streaming via Server-Sent Events (SSE).
"""

import asyncio
import json
import logging
import os
from typing import AsyncIterator

import aiohttp

from dragon_voice.config import LLMConfig
from dragon_voice.llm.base import LLMBackend

logger = logging.getLogger(__name__)


class OpenRouterBackend(LLMBackend):
    """LLM backend using the OpenRouter API."""

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._base_url = config.openrouter_url.rstrip("/")
        self._model = config.openrouter_model
        self._api_key = config.openrouter_api_key or os.environ.get(
            "OPENROUTER_API_KEY", ""
        )
        self._session: aiohttp.ClientSession | None = None
        self._conversation: list[dict] = []
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Verify API key is set and connectivity is available."""
        if not self._api_key:
            raise ValueError(
                "OpenRouter API key not configured. Set openrouter_api_key in "
                "config.yaml or OPENROUTER_API_KEY environment variable."
            )

        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120, sock_read=60),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://tinkerclaw.local",
                "X-Title": "TinkerClaw Dragon Voice",
            },
        )

        logger.info(
            "Initializing OpenRouter backend — model=%s, url=%s",
            self._model,
            self._base_url,
        )

        # Quick connectivity check
        try:
            async with self._session.get(f"{self._base_url}/models") as resp:
                if resp.status == 200:
                    logger.info("OpenRouter API reachable")
                else:
                    logger.warning("OpenRouter returned status %d", resp.status)
        except aiohttp.ClientError as e:
            logger.warning("Cannot reach OpenRouter: %s", e)

    async def _stream_messages(
        self, messages: list[dict], _retried: bool = False,
    ) -> AsyncIterator[str]:
        """Internal: stream tokens from OpenRouter given a full message list.

        If the API returns a context_length_exceeded error (HTTP 400), trims
        oldest non-system messages aggressively and retries once (US-P16).

        If the API returns HTTP 429 (rate limited), parses Retry-After header
        and waits before retrying once (A20). This handles thundering-herd
        scenarios where Dragon and TinkerClaw share an OpenRouter API key.
        """
        if self._session is None or self._session.closed:
            await self.initialize()

        payload = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "max_tokens": self._config.max_tokens,
            "temperature": self._config.temperature,
        }

        try:
            async with self._session.post(
                f"{self._base_url}/chat/completions",
                json=payload,
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(
                        "OpenRouter error %d: %s", resp.status, error_text[:300]
                    )

                    # Handle 429 rate limit: wait Retry-After and retry once (A20)
                    if resp.status == 429 and not _retried:
                        retry_after = resp.headers.get("Retry-After", "")
                        try:
                            wait_secs = float(retry_after) if retry_after else 2.0
                        except (ValueError, TypeError):
                            wait_secs = 2.0
                        # Cap the wait to 30s to avoid blocking the user forever
                        wait_secs = min(wait_secs, 30.0)
                        logger.warning(
                            "OpenRouter rate limited (429) — waiting %.1fs before retry",
                            wait_secs,
                        )
                        await asyncio.sleep(wait_secs)
                        async for token in self._stream_messages(messages, _retried=True):
                            yield token
                        return

                    # Handle context_length_exceeded: trim and retry once
                    if (resp.status == 400
                            and "context_length" in error_text.lower()
                            and not _retried
                            and len(messages) > 2):
                        logger.warning(
                            "Context length exceeded — trimming aggressively and retrying"
                        )
                        from dragon_voice.messages import trim_context_to_budget
                        # Halve the current estimated budget for aggressive trim
                        from dragon_voice.messages import estimate_tokens
                        current = sum(estimate_tokens(m.get("content", "")) for m in messages)
                        trimmed = trim_context_to_budget(messages, current // 2)
                        async for token in self._stream_messages(trimmed, _retried=True):
                            yield token
                        return

                    yield f"[OpenRouter error: {resp.status}]"
                    return

                # Parse SSE stream
                async for line in resp.content:
                    line = line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data: "):
                        continue

                    data_str = line[6:]  # Strip "data: " prefix
                    if data_str == "[DONE]":
                        break

                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    choices = chunk.get("choices", [])
                    if not choices:
                        continue

                    delta = choices[0].get("delta", {})
                    token = delta.get("content", "")
                    if token:
                        yield token

        except aiohttp.ClientError as e:
            logger.error("OpenRouter request failed: %s", e)
            yield f"[Connection error: {e}]"

    async def generate_stream(
        self, prompt: str, system_prompt: str = ""
    ) -> AsyncIterator[str]:
        """Stream tokens from OpenRouter using SSE.

        Uses the OpenAI-compatible /chat/completions endpoint.
        Maintains in-memory conversation history for the legacy (non-session) path.
        """
        sys_prompt = system_prompt or self._config.system_prompt

        async with self._lock:
            messages = []
            if sys_prompt:
                messages.append({"role": "system", "content": sys_prompt})
            messages.extend(self._conversation)
            messages.append({"role": "user", "content": prompt})

            full_response = []
            async for token in self._stream_messages(messages):
                full_response.append(token)
                yield token

            # Update in-memory conversation history (legacy path only)
            self._conversation.append({"role": "user", "content": prompt})
            self._conversation.append(
                {"role": "assistant", "content": "".join(full_response)}
            )

    async def generate_stream_with_messages(
        self, messages: list[dict]
    ) -> AsyncIterator[str]:
        """Stream tokens using a full OpenAI-format message list.

        Used by ConversationEngine which manages its own DB-backed context.
        Does NOT touch self._conversation — session isolation is handled by the caller.
        """
        async for token in self._stream_messages(messages):
            yield token

    def clear_history(self) -> None:
        """Clear conversation history."""
        self._conversation.clear()

    def trim_history(self, max_turns: int = 10) -> None:
        """Keep only the last N turns."""
        max_messages = max_turns * 2
        if len(self._conversation) > max_messages:
            self._conversation = self._conversation[-max_messages:]

    async def shutdown(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        logger.info("OpenRouter backend shut down")

    @property
    def name(self) -> str:
        return f"OpenRouter ({self._model})"
