"""LM Studio LLM backend.

Connects to a local LM Studio server running an OpenAI-compatible API.
Very similar to OpenRouter but targeting localhost with no auth required.
"""

import asyncio
import json
import logging
from typing import AsyncIterator

import aiohttp

from dragon_voice.config import LLMConfig
from dragon_voice.llm.base import LLMBackend, Modality

logger = logging.getLogger(__name__)


class LMStudioBackend(LLMBackend):
    """LLM backend using LM Studio's local OpenAI-compatible API."""

    # Slow CPU-local path (llama-server on Dragon, 60-90 s per summary):
    # synthesize the dictation title+summary from the transcript instead
    # of round-tripping the LLM, so the pipeline reaches SAVED before
    # Tab5's 45 s grace timer / the ngrok idle-close window.
    synthesize_summary_locally = True

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._base_url = config.lmstudio_url.rstrip("/")
        self._model = config.lmstudio_model
        self._session: aiohttp.ClientSession | None = None
        self._conversation: list[dict] = []
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Verify LM Studio is reachable."""
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=600, sock_read=300),
            headers={"Content-Type": "application/json"},
        )

        logger.info(
            "Initializing LM Studio backend — model=%s, url=%s",
            self._model,
            self._base_url,
        )

        try:
            async with self._session.get(f"{self._base_url}/models") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    models = [m.get("id", "") for m in data.get("data", [])]
                    logger.info("LM Studio reachable — models: %s", models[:5])
                else:
                    logger.warning(
                        "LM Studio returned status %d — may not be running",
                        resp.status,
                    )
        except aiohttp.ClientError as e:
            logger.warning(
                "Cannot reach LM Studio at %s: %s — will retry on first request",
                self._base_url,
                e,
            )

    async def generate_stream(
        self, prompt: str, system_prompt: str = ""
    ) -> AsyncIterator[str]:
        """Stream tokens from LM Studio using SSE."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=600, sock_read=300),
                headers={"Content-Type": "application/json"},
            )

        sys_prompt = system_prompt or self._config.system_prompt

        async with self._lock:
            messages = []
            if sys_prompt:
                messages.append({"role": "system", "content": sys_prompt})
            messages.extend(self._conversation)
            messages.append({"role": "user", "content": prompt})

            payload = {
                "model": self._model,
                "messages": messages,
                "stream": True,
                "max_tokens": self._config.max_tokens,
                "temperature": self._config.temperature,
            }

            full_response = []

            try:
                async with self._session.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error(
                            "LM Studio error %d: %s", resp.status, error_text[:300]
                        )
                        yield f"[LM Studio error: {resp.status}]"
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
                            full_response.append(token)
                            yield token

            except aiohttp.ClientError as e:
                logger.error("LM Studio request failed: %s", e)
                yield f"[Connection error: {e}]"
                return

            self._conversation.append({"role": "user", "content": prompt})
            self._conversation.append(
                {"role": "assistant", "content": "".join(full_response)}
            )

    async def generate_stream_with_messages(
        self, messages: list[dict]
    ) -> AsyncIterator[str]:
        """Stream tokens using a full OpenAI-format message list.

        LSP-1 fix (audit 2026-05-03): without this override, LMStudioBackend
        inherited `LLMBackend.generate_stream_with_messages` which formats
        messages via `f"{role}: {content}"` and re-routes through
        `generate_stream(prompt, system_prompt)`.  When the content of a
        user message is a multimodal array (e.g. `[{"type": "image_url",
        "image_url": {...}}, {"type": "text", "text": "what is this?"}]`),
        the f-string materialises the literal Python repr —
        `"User: [{'type': 'image_url', ...}]"` — and the model sees a
        string description of the array instead of the actual image.  Net
        effect: every router-driven vision turn that picked LM Studio
        silently became text-only with garbled JSON-as-text, and the
        request still came back with a confident wrong answer.

        LM Studio's `/chat/completions` is OpenAI-compatible natively; it
        already understands the multimodal content array shape.  This
        override just hands `messages` straight through.

        Used by ConversationEngine which manages its own DB-backed context.
        Does NOT touch self._conversation — session isolation is handled
        by the caller (matches the OpenRouter override at
        openrouter_llm.py:321).
        """
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=600, sock_read=300),
                headers={"Content-Type": "application/json"},
            )

        payload = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "max_tokens": self._config.max_tokens,
            "temperature": self._config.temperature,
        }

        async with self._lock:
            try:
                async with self._session.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error(
                            "LM Studio error %d: %s", resp.status, error_text[:300]
                        )
                        yield f"[LM Studio error: {resp.status}]"
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

            except aiohttp.ClientError as e:
                logger.error("LM Studio request failed: %s", e)
                yield f"[Connection error: {e}]"

    async def generate_with_tools(
        self, messages: list[dict], tools: list[dict],
        max_tokens: int | None = None,
        disable_thinking: bool = False,
        timeout_s: int | None = None,
    ) -> dict:
        """Native OpenAI tool-calling pass (SupportsNativeTools).

        Non-streaming. Sends `tools=[...]` + `tool_choice="auto"` at
        temperature 0 (deterministic tool routing) and returns the
        structured result. Requires llama-server started with `--jinja`
        so the loaded GGUF's chat template renders + parses tool calls.

        Returns {"content": str, "tool_calls": [{"name", "args"}, ...]}.
        On transport error returns content with an error marker and no
        tool calls so the caller degrades to a plain reply.
        """
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=600, sock_read=300),
                headers={"Content-Type": "application/json"},
            )

        # Cap the tool-decision generation hard. A tool call (or a 1-2
        # sentence reply) is short; without this the model can run toward
        # the 1024 MAX_TOOL_LOCAL ceiling and, non-streaming on a 1B CPU,
        # blow past the 300 s sock_read timeout (~0.2-0.3 s/token) — which
        # presented as "native turn fires no tool". 256 is plenty for a
        # tool call + clean args.
        payload = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "max_tokens": max_tokens or min(self._config.max_tokens, 256),
            "temperature": 0.0,
            "tools": tools,
            "tool_choice": "auto",
        }
        # Reasoning models (Qwen3.x) default to thinking, which burns the
        # token budget before emitting a tool call. The smart tier passes
        # disable_thinking=True; templates that don't accept the kwarg 400,
        # so we retry once without it.
        if disable_thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": False}

        _post_kw: dict = {}
        if timeout_s:
            _post_kw["timeout"] = aiohttp.ClientTimeout(
                total=timeout_s, sock_read=timeout_s
            )

        async def _do_post(pl):
            async with self._session.post(
                f"{self._base_url}/chat/completions", json=pl, **_post_kw,
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return None, resp.status, body
                return await resp.json(), 200, ""

        async with self._lock:
            try:
                data, status, body = await _do_post(payload)
                if data is None and status in (400, 500) and disable_thinking:
                    payload.pop("chat_template_kwargs", None)
                    data, status, body = await _do_post(payload)
                if data is None:
                    logger.error("LM Studio tools error %d: %s", status, body[:300])
                    return {"content": "", "tool_calls": []}
            except aiohttp.ClientError as e:
                logger.error("LM Studio tools request failed: %s", e)
                return {"content": "", "tool_calls": []}

        choices = data.get("choices") or [{}]
        msg = choices[0].get("message", {}) or {}
        calls: list[dict] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            name = fn.get("name")
            if not name:
                continue
            raw_args = fn.get("arguments")
            if isinstance(raw_args, dict):
                args = raw_args
            else:
                try:
                    args = json.loads(raw_args) if raw_args else {}
                except (json.JSONDecodeError, TypeError):
                    args = {}
            calls.append({"name": name, "args": args})

        return {"content": msg.get("content") or "", "tool_calls": calls}

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
        logger.info("LM Studio backend shut down")

    @property
    def name(self) -> str:
        return f"LM Studio ({self._model})"

    @property
    def capabilities(self) -> frozenset[Modality]:
        """Modalities for the configured LM Studio model.

        Delegates to the centralized capability registry (#200, Wave 21).
        See `dragon_voice/llm/capability_registry.py:detect_lmstudio` —
        same vision-hint substring set as ollama, but always declares
        TOOL_CALLING (the LM Studio server supports OpenAI-format tools
        across the board regardless of whether the loaded GGUF was
        trained for them).
        """
        from .capability_registry import detect

        return detect("lmstudio", self._model)
