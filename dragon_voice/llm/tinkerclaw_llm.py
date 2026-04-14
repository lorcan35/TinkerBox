"""TinkerClaw gateway LLM backend.

Routes LLM inference to the local TinkerClaw agent gateway via its
OpenAI-compatible /v1/chat/completions endpoint. TinkerClaw maintains
its own conversation state, skills, and memory — Dragon just pipes
audio and streams text.
"""

import asyncio
import json
import logging
from typing import AsyncIterator, Optional

import aiohttp

from dragon_voice.config import LLMConfig
from dragon_voice.llm.base import LLMBackend

logger = logging.getLogger(__name__)


class TinkerClawBackend(LLMBackend):
    """LLM backend that proxies to a local TinkerClaw agent gateway."""

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._url = (config.tinkerclaw_url or "http://localhost:18789").rstrip("/")
        self._token = config.tinkerclaw_token
        self._model = config.tinkerclaw_model or "ollama/qwen3:1.7b"
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_key: Optional[str] = None

    async def initialize(self) -> None:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120, sock_read=30),
            headers=headers,
        )

        # Health check — verify gateway is reachable (non-fatal if down)
        try:
            async with self._session.get(f"{self._url}/health") as resp:
                if resp.status == 200:
                    logger.info("TinkerClaw gateway connected at %s", self._url)
                else:
                    logger.warning("TinkerClaw health check returned %d", resp.status)
        except aiohttp.ClientError as e:
            logger.warning("TinkerClaw gateway not reachable at %s: %s (will retry on first request)", self._url, e)

    def set_session_key(self, session_key: str) -> None:
        """Set the session key for conversation continuity.

        Called by pipeline/server before each request. TinkerClaw uses this
        to maintain per-session conversation history internally.
        """
        self._session_key = session_key

    async def generate_stream(
        self, prompt: str, system_prompt: str = ""
    ) -> AsyncIterator[str]:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        async for token in self.generate_stream_with_messages(messages):
            yield token

    async def generate_stream_with_messages(
        self, messages: list[dict]
    ) -> AsyncIterator[str]:
        """Stream tokens from TinkerClaw via OpenAI-compatible SSE.

        Dragon sends only the latest user message. TinkerClaw maintains
        full conversation history internally via the session key.
        """
        if not self._session or self._session.closed:
            await self.initialize()

        payload = {
            "model": self._model,
            "messages": messages,
            "stream": True,
        }
        if self._session_key:
            payload["user"] = self._session_key

        try:
            async with self._session.post(
                f"{self._url}/v1/chat/completions",
                json=payload,
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error("TinkerClaw error %d: %s", resp.status, error_text[:300])
                    yield "Sorry, my agent system returned an error. Please try again."
                    return

                async for line in resp.content:
                    line = line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data: "):
                        continue

                    data_str = line[6:]
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

        except asyncio.TimeoutError:
            logger.error("TinkerClaw SSE stream timed out (sock_read=30s)")
            yield "Response timed out, please try again."
        except aiohttp.ClientError as e:
            logger.error("TinkerClaw request failed: %s", e)
            yield "I'm having trouble connecting to my agent system. Please try again in a moment."

    async def shutdown(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        logger.info("TinkerClaw backend shut down")

    @property
    def name(self) -> str:
        return f"TinkerClaw ({self._model})"
