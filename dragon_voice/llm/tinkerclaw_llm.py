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
        # Audit J12/J20/K13 (wave 7): align the empty-config fallback with
        # dragon_voice.config.LLMConfig.tinkerclaw_model default
        # ("minimax/MiniMax-M2.5") and with the TinkerTab audit expectation.
        # The old "ollama/qwen3:1.7b" fallback was a three-layer mismatch
        # (Dragon fallback ≠ gateway default ≠ Tab5 expectation) so users
        # could think they were talking to MiniMax while actually getting
        # whatever the gateway chose from its own config.
        self._model = config.tinkerclaw_model or "minimax/MiniMax-M2.5"
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_key: Optional[str] = None

    async def initialize(self) -> None:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        self._session = aiohttp.ClientSession(
            # P05: sock_read raised from 30s to 90s. TinkerClaw tool execution
            # (web_search, memory recall, etc.) can take 10-30s with no SSE
            # data flowing. The total=180s covers the full agent run including
            # multiple tool rounds.
            timeout=aiohttp.ClientTimeout(total=180, sock_read=90),
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

        Truncation detection (A07): If the SSE stream ends without a
        [DONE] marker (e.g. TinkerClaw crash, sqlite-vec segfault, network
        drop), we append a truncation indicator so the user knows the
        response is incomplete rather than silently sending a mid-sentence
        fragment to TTS.

        Tool-calling note (P05): TinkerClaw sends a SINGLE SSE stream for
        the entire agent run. Tool calls happen internally — the agent
        runner executes tools and then streams the final assistant text,
        all within one HTTP response. There is only one [DONE] at the very
        end. During tool execution there may be a long gap (10-30s) with
        no content deltas; the sock_read timeout (set below) must be long
        enough to cover this. Dragon's parser already handles this correctly:
        tool_call deltas have empty "content" and are silently skipped,
        then the final text deltas are yielded normally.
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

                saw_done = False
                token_count = 0
                consecutive_errors = 0  # P11: detect HTML error pages from proxy

                async for line in resp.content:
                    line = line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data: "):
                        continue

                    data_str = line[6:]
                    if data_str == "[DONE]":
                        saw_done = True
                        break

                    try:
                        chunk = json.loads(data_str)
                        consecutive_errors = 0  # Reset on successful parse
                    except json.JSONDecodeError:
                        consecutive_errors += 1
                        if consecutive_errors >= 5:
                            logger.error(
                                "P11: %d consecutive JSON parse failures — "
                                "probable HTML error page from proxy. Last line: %s",
                                consecutive_errors, data_str[:200],
                            )
                            yield "Sorry, the connection returned an error page instead of a response. Please try again."
                            return
                        continue

                    choices = chunk.get("choices", [])
                    if not choices:
                        continue

                    delta = choices[0].get("delta", {})
                    token = delta.get("content", "")
                    if token:
                        token_count += 1
                        yield token

                # A07: Truncation detection — stream ended without [DONE]
                if not saw_done:
                    if token_count > 0:
                        logger.warning(
                            "SSE stream ended without [DONE] after %d tokens "
                            "— response may be truncated (TinkerClaw crash?)",
                            token_count,
                        )
                        yield " ... (response interrupted)"
                    else:
                        logger.warning(
                            "SSE stream ended without [DONE] and 0 tokens "
                            "— connection may have dropped before any response"
                        )
                        yield "I'm having trouble connecting to my agent system. Please try again in a moment."

        except asyncio.TimeoutError:
            logger.error("TinkerClaw SSE stream timed out (sock_read=90s)")
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
