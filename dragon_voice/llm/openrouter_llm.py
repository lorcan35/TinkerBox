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
        self._idempotency_key: str = ""
        # Phase 3 cost tracking. Populated from the last SSE chunk of each
        # stream (OpenAI / OpenRouter emit usage totals in the tail chunk
        # when stream_options.include_usage=true). Read by pipeline.py to
        # compute per-turn cost for receipt emission.
        self._last_usage: dict = {}
        # v4·D Gauntlet G2 fix: flag when the last turn went through a
        # context-trim retry (429 or context_length_exceeded).  pipeline
        # picks this up and stamps the receipt with retried=true so the
        # chat bubble can render a "RETRIED" chip.  Cleared at the top of
        # every new _stream_messages call.
        self._last_retried: bool = False
        self._last_retry_reason: str = ""

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
            "stream_options": {"include_usage": True},  # tail chunk carries totals
            "max_tokens": self._config.max_tokens,
            "temperature": self._config.temperature,
        }
        # Reset last usage + retry flags on every new turn so stale
        # figures from the previous turn don't leak into the receipt.
        # Preserve the flags through recursive retries though -- only
        # reset on the OUTER call (_retried is false).
        if not _retried:
            self._last_usage = {}
            self._last_retried = False
            self._last_retry_reason = ""
            # v4·D Gauntlet G10 fix: fresh idempotency key per OUTER turn.
            # Intentional retries (429, context_trim) reuse it so
            # OpenRouter dedupes.  Unintentional retries (aiohttp's TCP
            # retransmit wrap-around on flaky networks) ALSO reuse it --
            # the whole point is that a single logical LLM call produces
            # a single billable response, regardless of transport shenanigans.
            import uuid as _uuid
            self._idempotency_key = str(_uuid.uuid4())

        try:
            async with self._session.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers={"Idempotency-Key": self._idempotency_key},
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
                        self._last_retried = True
                        self._last_retry_reason = "rate_limit"
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
                        self._last_retried = True
                        self._last_retry_reason = "context_trim"
                        async for token in self._stream_messages(trimmed, _retried=True):
                            yield token
                        return

                    yield f"[OpenRouter error: {resp.status}]"
                    return

                # Parse SSE stream
                consecutive_errors = 0  # P11: detect HTML error pages from proxy
                async for line in resp.content:
                    line = line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data: "):
                        continue

                    data_str = line[6:]  # Strip "data: " prefix
                    if data_str == "[DONE]":
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
                            yield "[Connection error: proxy returned an error page]"
                            return
                        continue

                    # Usage tail chunk: choices=[] + usage={...} present.
                    # Capture and move on -- yield nothing (no user-visible
                    # text).  Must come before the choices[] guard below or
                    # this chunk gets silently skipped.
                    usage = chunk.get("usage")
                    if usage:
                        self._last_usage = {
                            "model": self._model,
                            "prompt_tokens":     int(usage.get("prompt_tokens", 0)),
                            "completion_tokens": int(usage.get("completion_tokens", 0)),
                            "total_tokens":      int(usage.get("total_tokens", 0)),
                        }

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

    def get_last_usage(self) -> dict:
        """Return usage dict from the most recent streamed turn.

        Keys (when populated): model, prompt_tokens, completion_tokens, total_tokens.
        Returns {} if no turn has run or the tail chunk lacked usage (e.g.
        the stream was aborted before the final SSE event).
        """
        u = dict(self._last_usage)
        if self._last_retried:
            u["retried"] = True
            u["retry_reason"] = self._last_retry_reason
        return u

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


# ── Pricing table (Phase 3) ───────────────────────────────────────────
# Values are in MILS per 1M tokens ($USD * 1000 * 1000).  Storing in mils
# keeps integer math accurate at 0.0001 USD precision.  Multiplying by
# token_count / 1_000_000 gives cost in mils.  Display side (Tab5) divides
# by 1000 for cents or 100000 for dollars.
#
# Prices match OpenRouter's listed rates as of April 2026.  Update in one
# place when they change.  Unknown models fall through to a conservative
# default so a pricing surprise never zeroes out the receipt.
_PRICING_MILS_PER_M = {
    # OpenAI
    "openai/gpt-4o":             {"in":   2500000, "out":  10000000},
    "openai/gpt-4o-mini":        {"in":    150000, "out":    600000},
    "openai/gpt-audio-mini":     {"in":    300000, "out":   1200000},
    # Anthropic
    "anthropic/claude-sonnet-4-20250514": {"in": 3000000, "out": 15000000},
    "anthropic/claude-3-haiku":  {"in":    250000, "out":   1250000},
    "anthropic/claude-3.5-haiku":{"in":    800000, "out":   4000000},
    # Fallback for anything unknown -- $2/$8 per M tokens, slightly high on purpose
    "_default":                  {"in":   2000000, "out":   8000000},
}


def price_for_model(model: str, prompt_tokens: int, completion_tokens: int) -> int:
    """Compute cost in MILS (1000ths of a USD cent) for a given model + usage.

    Returns an integer so it round-trips cleanly through JSON WS messages.
    Unknown cloud models use the conservative default table entry.  LOCAL
    models (ollama, npu_genie, etc) return 0 -- we detect them by the
    absence of a "vendor/" prefix which all OpenRouter-hosted IDs carry.
    """
    if not model:
        return 0
    # Local models (qwen3:1.7b, llama3.2, etc.) have no slash and run
    # on-device at zero marginal USD cost.  Short-circuit before any
    # pricing lookup so we never report phantom cloud rates for them.
    if "/" not in model:
        return 0
    entry = _PRICING_MILS_PER_M.get(model) or _PRICING_MILS_PER_M["_default"]
    # Compute as (tokens * mils_per_M) // 1_000_000 to stay integer.
    cost_in  = (int(prompt_tokens)     * entry["in"])  // 1_000_000
    cost_out = (int(completion_tokens) * entry["out"]) // 1_000_000
    return cost_in + cost_out
