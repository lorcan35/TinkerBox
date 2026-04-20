"""Ollama LLM backend.

Connects to a local Ollama server via its REST API. Supports streaming
token generation and conversation history management.
"""

import asyncio
import json
import logging
from typing import AsyncIterator

import aiohttp

from dragon_voice.config import LLMConfig
from dragon_voice.llm.base import LLMBackend

logger = logging.getLogger(__name__)


class OllamaBackend(LLMBackend):
    """LLM backend using Ollama's REST API."""

    # How long Ollama keeps a model loaded in memory after the last request.
    # Default is 5 minutes which causes OOM on 8GB Dragon when switching
    # between models (e.g. qwen3:1.7b -> qwen3:4b).  30s is enough to
    # avoid reload latency for back-to-back requests while freeing RAM
    # fast enough to prevent dual-model memory spikes.
    KEEP_ALIVE = "30s"

    def _capture_usage(self, chunk: dict) -> None:
        """Populate self._last_usage from a done=true stream chunk.
        Ollama's /api/chat tail chunk carries prompt_eval_count (input
        tokens) and eval_count (output tokens).  Local inference has
        zero marginal $ cost, but capturing the counts gives the UI
        something to show in the chat receipt stamp ("qwen3 · FREE"). """
        self._last_usage = {
            "model": self._model,
            "prompt_tokens":     int(chunk.get("prompt_eval_count", 0) or 0),
            "completion_tokens": int(chunk.get("eval_count",        0) or 0),
        }
        self._last_usage["total_tokens"] = (
            self._last_usage["prompt_tokens"]
            + self._last_usage["completion_tokens"]
        )

    def get_last_usage(self) -> dict:
        return dict(getattr(self, "_last_usage", {}))

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._base_url = config.ollama_url.rstrip("/")
        self._model = config.ollama_model
        self._session: aiohttp.ClientSession | None = None
        self._last_usage: dict = {}
        self._conversation: list[dict] = []
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Verify Ollama is reachable and the model is available."""
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=300, sock_read=120)
        )

        logger.info(
            "Initializing Ollama backend — url=%s, model=%s",
            self._base_url,
            self._model,
        )

        try:
            async with self._session.get(f"{self._base_url}/api/tags") as resp:
                if resp.status != 200:
                    logger.warning(
                        "Ollama returned status %d — server may not be ready",
                        resp.status,
                    )
                    return

                data = await resp.json()
                models = [m["name"] for m in data.get("models", [])]

                # Normalize model names for comparison (strip :latest tag)
                model_base = self._model.split(":")[0]
                available = [m.split(":")[0] for m in models]

                if model_base not in available:
                    logger.warning(
                        "Model '%s' not found in Ollama. Available: %s. "
                        "Will attempt to pull on first request.",
                        self._model,
                        models[:10],
                    )
                else:
                    logger.info("Ollama model '%s' is available", self._model)

        except aiohttp.ClientError as e:
            logger.warning(
                "Cannot reach Ollama at %s: %s — will retry on first request",
                self._base_url,
                e,
            )

    async def generate_stream(
        self, prompt: str, system_prompt: str = ""
    ) -> AsyncIterator[str]:
        """Stream tokens from Ollama using the /api/chat endpoint.

        Maintains conversation history for multi-turn dialogue.
        """
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_read=None)
            )

        sys_prompt = system_prompt or self._config.system_prompt

        async with self._lock:
            # Build messages list with history
            messages = []
            if sys_prompt:
                messages.append({"role": "system", "content": sys_prompt})
            messages.extend(self._conversation)
            messages.append({"role": "user", "content": prompt})

            payload = {
                "model": self._model,
                "messages": messages,
                "stream": True,
                "keep_alive": self.KEEP_ALIVE,
                "options": {
                    "num_predict": self._config.max_tokens,
                    "temperature": self._config.temperature,
                },
            }

            full_response = []

            try:
                resp_ctx = self._session.post(
                    f"{self._base_url}/api/chat",
                    json=payload,
                )
                resp = await asyncio.wait_for(resp_ctx.__aenter__(), timeout=300)
                try:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error("Ollama error %d: %s", resp.status, error_text[:200])
                        yield f"[Ollama error: {resp.status}]"
                        return

                    async for raw_line in resp.content:
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        if chunk.get("done"):
                            self._capture_usage(chunk)
                            break

                        token = chunk.get("message", {}).get("content", "")
                        if token:
                            full_response.append(token)
                            yield token
                finally:
                    await resp_ctx.__aexit__(None, None, None)

            except asyncio.TimeoutError:
                logger.error("Ollama generation timed out after 300s")
                yield "[Ollama timeout after 300s]"
                return
            except aiohttp.ClientError as e:
                logger.error("Ollama request failed: %s", e)
                yield f"[Connection error: {e}]"
                return

            # Update conversation history
            self._conversation.append({"role": "user", "content": prompt})
            self._conversation.append(
                {"role": "assistant", "content": "".join(full_response)}
            )

    async def generate_stream_with_messages(
        self, messages: list[dict]
    ) -> AsyncIterator[str]:
        """Native multi-turn using Ollama /api/chat with full message context."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_read=None)
            )

        payload = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "keep_alive": self.KEEP_ALIVE,
            "options": {
                "num_predict": self._config.max_tokens,
                "temperature": self._config.temperature,
            },
        }

        try:
            resp_ctx = self._session.post(
                f"{self._base_url}/api/chat",
                json=payload,
            )
            resp = await asyncio.wait_for(resp_ctx.__aenter__(), timeout=300)
            try:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error("Ollama error %d: %s", resp.status, error_text[:200])
                    yield f"[Ollama error: {resp.status}]"
                    return

                async for raw_line in resp.content:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if chunk.get("done"):
                        self._capture_usage(chunk)
                        break

                    token = chunk.get("message", {}).get("content", "")
                    if token:
                        yield token
            finally:
                await resp_ctx.__aexit__(None, None, None)

        except asyncio.TimeoutError:
            logger.error("Ollama generation timed out after 300s")
            yield "[Ollama timeout after 300s]"
        except aiohttp.ClientError as e:
            logger.error("Ollama request failed: %s", e)
            yield f"[Connection error: {e}]"

    def clear_history(self) -> None:
        """Clear conversation history."""
        self._conversation.clear()
        logger.debug("Ollama conversation history cleared")

    def trim_history(self, max_turns: int = 10) -> None:
        """Keep only the last N turns (each turn = user + assistant)."""
        max_messages = max_turns * 2
        if len(self._conversation) > max_messages:
            self._conversation = self._conversation[-max_messages:]

    async def shutdown(self) -> None:
        """Shut down the Ollama backend, closing the aiohttp session.

        Uses a 5-second timeout to prevent hanging when Ollama is mid-inference
        (A14). aiohttp session.close() cancels in-flight requests, but the TCP
        teardown can stall if the streaming response is blocked in a kernel
        buffer. After timeout, we force-close the underlying connector.
        """
        if self._session and not self._session.closed:
            try:
                await asyncio.wait_for(self._session.close(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "Ollama session.close() timed out after 5s (mid-inference?) "
                    "— force-closing connector"
                )
                # Force-close the underlying connector to drop TCP connections
                if self._session.connector and not self._session.connector.closed:
                    self._session.connector.close()
            except Exception as e:
                logger.warning("Ollama session close error: %s", e)
        self._session = None
        logger.info("Ollama backend shut down")

    @property
    def name(self) -> str:
        return f"Ollama ({self._model})"
