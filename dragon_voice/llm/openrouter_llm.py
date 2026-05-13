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
from dragon_voice.llm.base import LLMBackend, Modality

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

    async def health_check(self, timeout_s: float = 2.0) -> tuple[bool, str]:
        """W4-B: cheap reachability probe — `GET {base_url}/models`.

        Bearer-authed via the session's stored Authorization header.  A
        non-200 surfaces as not-ok with the status code; auth misconfig
        bubbles up here too (401 → operators see it in /health).
        """
        if not self._api_key:
            return False, "no api key"
        if self._session is None or self._session.closed:
            return False, "session not initialized"
        try:
            async with self._session.get(
                f"{self._base_url}/models",
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as resp:
                if resp.status == 200:
                    return True, f"{self._base_url}"
                return False, f"HTTP {resp.status}"
        except asyncio.TimeoutError:
            return False, f"timeout after {timeout_s}s"
        except aiohttp.ClientError as e:
            return False, str(e)[:120]
        except Exception as e:  # noqa: BLE001 — must never raise
            return False, f"{type(e).__name__}: {e}"[:120]

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
        # v4·D audit P2 fix: initialize() makes a blocking HTTPS call
        # to /models to validate the API key.  On every recursive SSE
        # failure that hits this branch (session closed mid-stream) we
        # were re-pinging OpenRouter from inside the hot path.  Only
        # re-init when genuinely missing; on closed-after-init just
        # rebuild the aiohttp session without re-validating the key.
        if self._session is None:
            await self.initialize()
        elif self._session.closed:
            # Rebuild the aiohttp session without re-validating the API
            # key against /models (initialize() does that expensively).
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120, sock_read=60),
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://tinkerclaw.local",
                    "X-Title": "TinkerClaw Dragon Voice",
                },
            )

        # Audit follow-up (2026-04-20): Gemini-via-OpenRouter rejects
        # role="tool" messages missing tool_call_id with HTTP 400
        # ("Tool message must have either name or tool_call_id"). We
        # don't carry a tool_call_id through the history (tools are
        # stored as XML-tagged assistant content), so the safest cross-
        # provider fix is to rewrite tool-role messages into user-role
        # messages with a "(tool output) ..." prefix. Works for
        # Anthropic, OpenAI, Gemini equally. Non-tool roles pass through.
        sanitized = []
        for _msg in messages:
            if _msg.get("role") == "tool" and not _msg.get("tool_call_id"):
                sanitized.append({
                    "role": "user",
                    "content": f"(tool output) {_msg.get('content', '')}",
                })
            else:
                sanitized.append(_msg)
        messages = sanitized

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

                    # v4·D audit P0 fix: do NOT yield the raw error
                    # string into the token stream.  It used to end up
                    # in conversation history AND get spoken by TTS
                    # ("open router error 500").  Log + yield a
                    # user-friendly fallback sentence instead.  The
                    # retry flag lets the receipt stamp a "retried"
                    # chip so users know something went sideways.
                    logger.error("OpenRouter error %d: %s",
                                 resp.status, (error_text or "")[:200])
                    self._last_retried = True
                    self._last_retry_reason = f"openrouter_{resp.status}"
                    yield "Sorry, the cloud model had a hiccup. Try again?"
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
                            self._last_retried = True
                            self._last_retry_reason = "proxy_html_error"
                            yield "Sorry, the cloud proxy threw an error. Try again?"
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
            self._last_retried = True
            self._last_retry_reason = f"client_error: {type(e).__name__}"
            yield "Sorry, I couldn't reach the cloud. Try again in a moment?"

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

    @property
    def capabilities(self) -> frozenset[Modality]:
        """Modalities for the configured OpenRouter model.

        Delegates to the centralized capability registry (#200, Wave 21),
        which routes through `_openrouter_capabilities()` below — keeping
        the OR-specific static dict (`_OPENROUTER_CAPS`, ~35 entries) in
        this file but accessed through the unified registry interface.
        """
        from .capability_registry import detect

        return detect("openrouter", self._model)


# ── Pricing table (Phase 3) ───────────────────────────────────────────
# Values are in MILS per 1M tokens ($USD * 1000 * 1000).  Storing in mils
# keeps integer math accurate at 0.0001 USD precision.  Multiplying by
# token_count / 1_000_000 gives cost in mils.  Display side (Tab5) divides
# by 1000 for cents or 100000 for dollars.
#
# Prices match OpenRouter's listed rates as of 2026-04-27 (verified via
# the live /api/v1/models endpoint).  Update in one place when they
# change.  Unknown models fall through to a conservative default so a
# pricing surprise never zeroes out the receipt.
_PRICING_MILS_PER_M = {
    # ── OpenAI ─────────────────────────────────────────────────────
    "openai/gpt-4o":              {"in":  2500000, "out":  10000000},
    "openai/gpt-4o-mini":         {"in":   150000, "out":    600000},
    "openai/gpt-audio-mini":      {"in":   300000, "out":   1200000},
    "openai/gpt-5.5":             {"in":  5000000, "out":  30000000},  # Apr 24
    "openai/gpt-5.5-pro":         {"in": 30000000, "out": 180000000},
    "openai/gpt-5.4":             {"in":  2500000, "out":  15000000},
    "openai/gpt-5.4-mini":        {"in":   750000, "out":   4500000},
    "openai/gpt-5.4-nano":        {"in":   200000, "out":   1250000},
    # ── Anthropic ──────────────────────────────────────────────────
    "anthropic/claude-sonnet-4-20250514": {"in": 3000000, "out": 15000000},
    "anthropic/claude-3-haiku":   {"in":   250000, "out":   1250000},
    "anthropic/claude-3.5-haiku": {"in":   800000, "out":   4000000},
    "anthropic/claude-haiku-4.5": {"in":  1000000, "out":   5000000},  # Oct 15
    "anthropic/claude-sonnet-4.5":{"in":  3000000, "out":  15000000},  # Sep 29
    "anthropic/claude-sonnet-4.6":{"in":  3000000, "out":  15000000},  # Feb 17
    "anthropic/claude-opus-4.5":  {"in":  5000000, "out":  25000000},
    "anthropic/claude-opus-4.6":  {"in":  5000000, "out":  25000000},
    "anthropic/claude-opus-4.7":  {"in":  5000000, "out":  25000000},  # Apr 16
    # ── Google ─────────────────────────────────────────────────────
    "google/gemini-3-flash-preview":      {"in":  500000, "out":  3000000},  # Dec 17
    "google/gemini-3.1-pro-preview":      {"in": 2000000, "out": 12000000},  # Feb 19
    "google/gemini-3.1-flash-lite-preview":{"in": 250000, "out":  1500000},
    "google/gemini-2.5-flash":            {"in":  300000, "out":  2500000},
    "google/gemini-2.5-flash-lite":       {"in":  100000, "out":   400000},
    "google/gemini-2.0-flash-001":        {"in":  100000, "out":   400000},
    "google/gemma-4-26b-a4b-it":          {"in":   60000, "out":   330000},  # Apr 3
    "google/gemma-4-31b-it":              {"in":  130000, "out":   380000},  # Apr 2
    # ── DeepSeek ───────────────────────────────────────────────────
    "deepseek/deepseek-v3.2":     {"in":   250000, "out":    380000},
    "deepseek/deepseek-v4-flash": {"in":   140000, "out":    280000},  # Apr 24
    "deepseek/deepseek-v4-pro":   {"in":   430000, "out":    870000},  # Apr 24
    "deepseek/deepseek-r1":       {"in":   700000, "out":   2500000},
    # ── Qwen 3.5 / 3.6 family ──────────────────────────────────────
    "qwen/qwen3.6-flash":         {"in":   250000, "out":  1500000},  # Apr 27 (released today)
    "qwen/qwen3.6-27b":           {"in":   500000, "out":  2000000},
    "qwen/qwen3.6-35b-a3b":       {"in":   160000, "out":   970000},
    "qwen/qwen3.6-max-preview":   {"in":  1300000, "out":  7800000},
    "qwen/qwen3.6-plus":          {"in":   330000, "out":  1950000},
    "qwen/qwen3.5-plus-20260420": {"in":   400000, "out":  2400000},
    "qwen/qwen3.5-flash-02-23":   {"in":    70000, "out":   260000},
    # ── Moonshot / Kimi ────────────────────────────────────────────
    "moonshotai/kimi-k2.6":       {"in":   740000, "out":  4660000},  # Apr 20
    "moonshotai/kimi-k2.5":       {"in":   440000, "out":  2000000},
    # ── x-AI Grok ──────────────────────────────────────────────────
    "x-ai/grok-4.20":             {"in":  2000000, "out":  6000000},
    "x-ai/grok-4.20-multi-agent": {"in":  2000000, "out":  6000000},
    "x-ai/grok-4.1-fast":         {"in":   200000, "out":   500000},
    # ── Z.ai GLM ───────────────────────────────────────────────────
    "z-ai/glm-5.1":               {"in":  1050000, "out":  3500000},
    "z-ai/glm-5v-turbo":          {"in":  1200000, "out":  4000000},
    "z-ai/glm-4.7-flash":         {"in":    60000, "out":   400000},
    # ── Xiaomi MiMo ────────────────────────────────────────────────
    "xiaomi/mimo-v2.5":           {"in":   400000, "out":  2000000},
    "xiaomi/mimo-v2.5-pro":       {"in":  1000000, "out":  3000000},
    # Fallback for anything unknown -- $2/$8 per M tokens, slightly high on purpose
    "_default":                   {"in":  2000000, "out":  8000000},
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
    # v4·D audit P1 fix: ceil-division so a tiny turn never floors to
    # zero mils.  Previously a 2-token Haiku turn computed to 0 mils
    # via floor-div, silently undercounting the daily spend.  With
    # ceil-div, any billable turn costs at least 1 mil.
    in_num  = int(prompt_tokens)     * entry["in"]
    out_num = int(completion_tokens) * entry["out"]
    cost_in  = (in_num  + 999_999) // 1_000_000 if in_num  > 0 else 0
    cost_out = (out_num + 999_999) // 1_000_000 if out_num > 0 else 0
    return cost_in + cost_out


# Per-frame token-count approximation for vision-capable models.  Different
# vendors charge differently (gpt-4o tile-based ~480 tokens for a typical
# detail tile, Sonnet ~1500, Gemini ~258 fixed) but 1500 is a reasonable
# midpoint that matches what the prior inline switch in server.py was
# implicitly assuming for sonnet/haiku/gemini; gpt-4o ends up ~3× over-
# charged which is fine — over-counting is the safe direction since the
# daily cap exists to STOP spending, not to track to-the-mil precision.
_VISION_TOKENS_PER_FRAME_APPROX = 1500


def vision_per_frame_mils(model: str) -> int:
    """Estimate the per-frame mils cost of a vision turn for ``model``.

    OCP-2 fix (audit 2026-05-03): the prior inline switch in
    ``server.py:_handle_config_update`` hardcoded prices for
    ``gpt-4o``/``sonnet``/``haiku``/``gemini`` and silently defaulted
    Opus 4.x, Grok 4.x, Kimi, Qwen 3.6, GLM, MiMo etc. to **0 mils per
    frame** — so every cloud-vision turn on those models was invisible
    to the daily-cap budget enforcement.

    This helper routes through :func:`price_for_model` with a fixed
    1500-token-per-frame approximation so any model added to the
    canonical ``_PRICING_MILS_PER_M`` registry automatically gets
    real per-frame pricing without a parallel switch.

    Local models (no ``/`` in the id) cost zero — they run on-device.
    Unknown cloud models fall through to the ``_default`` pricing entry
    in the registry, which is conservatively high — never zero.

    Returns mils as an int so it serialises cleanly through WS JSON.
    """
    return price_for_model(
        model,
        prompt_tokens=_VISION_TOKENS_PER_FRAME_APPROX,
        completion_tokens=0,
    )


# ── Capability registry (#183) ─────────────────────────────────────────
# Per-model declared modalities for the multi-model router.  Each entry
# is the set of caps OpenRouter exposes for that model.  Keep in sync
# with OpenRouter's documented multimodal endpoints; new entries added
# as we use new models.
#
# Default for unknown OR models: {TEXT, TOOL_CALLING}.  OR's chat
# completions API supports tools across the board; vision/video/audio
# are explicit per-model and must be opted in.
_OPENROUTER_CAPS: dict[str, frozenset[Modality]] = {
    # ── Anthropic ────────────────────────────────────────────────
    # Note: claude-3.5-haiku is technically vision-capable per the
    # Anthropic route, but OR's Bedrock route rejects images.  Until
    # OR adds a way to pin the route, we conservatively declare it
    # text-only so the router doesn't pick it for vision turns.
    "anthropic/claude-3.5-haiku":         frozenset({Modality.TEXT, Modality.TOOL_CALLING}),
    "anthropic/claude-3-haiku":           frozenset({Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING}),
    "anthropic/claude-haiku-4.5":         frozenset({Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING}),  # Oct 15
    "anthropic/claude-sonnet-4-20250514": frozenset({Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING}),
    "anthropic/claude-sonnet-4.5":        frozenset({Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING}),  # Sep 29
    "anthropic/claude-sonnet-4.6":        frozenset({Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING}),  # Feb 17
    "anthropic/claude-opus-4.5":          frozenset({Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING}),
    "anthropic/claude-opus-4.6":          frozenset({Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING}),
    "anthropic/claude-opus-4.7":          frozenset({Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING}),  # Apr 16
    # ── OpenAI ───────────────────────────────────────────────────
    "openai/gpt-4o":                      frozenset({Modality.TEXT, Modality.VISION, Modality.AUDIO_IN, Modality.AUDIO_OUT, Modality.TOOL_CALLING}),
    "openai/gpt-4o-mini":                 frozenset({Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING}),
    "openai/gpt-audio-mini":              frozenset({Modality.TEXT, Modality.AUDIO_IN, Modality.AUDIO_OUT}),
    "openai/gpt-5.4":                     frozenset({Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING}),
    "openai/gpt-5.4-mini":                frozenset({Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING}),
    "openai/gpt-5.4-nano":                frozenset({Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING}),
    "openai/gpt-5.5":                     frozenset({Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING}),  # Apr 24
    "openai/gpt-5.5-pro":                 frozenset({Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING}),
    # ── Google Gemini family — native video + audio_in on 3.x ──
    "google/gemini-3-flash-preview":      frozenset({Modality.TEXT, Modality.VISION, Modality.VIDEO, Modality.AUDIO_IN, Modality.TOOL_CALLING}),
    "google/gemini-3.1-pro-preview":      frozenset({Modality.TEXT, Modality.VISION, Modality.VIDEO, Modality.AUDIO_IN, Modality.TOOL_CALLING}),
    "google/gemini-3.1-flash-lite-preview": frozenset({Modality.TEXT, Modality.VISION, Modality.VIDEO, Modality.AUDIO_IN, Modality.TOOL_CALLING}),
    "google/gemini-2.5-flash":            frozenset({Modality.TEXT, Modality.VISION, Modality.VIDEO, Modality.AUDIO_IN, Modality.TOOL_CALLING}),
    "google/gemini-2.5-flash-lite":       frozenset({Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING}),
    "google/gemini-2.0-flash-001":        frozenset({Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING}),
    # Gemma open-weights (vision capable since Gemma 3)
    "google/gemma-4-26b-a4b-it":          frozenset({Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING}),
    "google/gemma-4-31b-it":              frozenset({Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING}),
    # ── DeepSeek (text-only across the line) ────────────────────
    "deepseek/deepseek-v3.2":             frozenset({Modality.TEXT, Modality.TOOL_CALLING}),
    "deepseek/deepseek-v4-flash":         frozenset({Modality.TEXT, Modality.TOOL_CALLING}),  # Apr 24
    "deepseek/deepseek-v4-pro":           frozenset({Modality.TEXT, Modality.TOOL_CALLING}),  # Apr 24
    "deepseek/deepseek-r1":               frozenset({Modality.TEXT, Modality.TOOL_CALLING}),
    # ── Qwen 3.5 / 3.6 — multimodal across most of the line ────
    "qwen/qwen3.6-flash":                 frozenset({Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING}),  # Apr 27
    "qwen/qwen3.6-27b":                   frozenset({Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING}),
    "qwen/qwen3.6-35b-a3b":               frozenset({Modality.TEXT, Modality.VISION}),  # vision but no tools per OR meta
    "qwen/qwen3.6-max-preview":           frozenset({Modality.TEXT, Modality.TOOL_CALLING}),  # text+tools, no vision per OR meta
    "qwen/qwen3.6-plus":                  frozenset({Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING}),
    "qwen/qwen3.5-plus-20260420":         frozenset({Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING}),
    "qwen/qwen3.5-flash-02-23":           frozenset({Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING}),
    # ── Moonshot Kimi ───────────────────────────────────────────
    "moonshotai/kimi-k2.6":               frozenset({Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING}),
    "moonshotai/kimi-k2.5":               frozenset({Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING}),
    # ── x-AI Grok ───────────────────────────────────────────────
    "x-ai/grok-4.20":                     frozenset({Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING}),
    "x-ai/grok-4.1-fast":                 frozenset({Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING}),
    # ── Z.ai GLM ────────────────────────────────────────────────
    "z-ai/glm-5.1":                       frozenset({Modality.TEXT, Modality.TOOL_CALLING}),
    "z-ai/glm-5v-turbo":                  frozenset({Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING}),
    "z-ai/glm-4.7-flash":                 frozenset({Modality.TEXT, Modality.TOOL_CALLING}),
    # ── Xiaomi MiMo ─────────────────────────────────────────────
    "xiaomi/mimo-v2.5":                   frozenset({Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING}),
    "xiaomi/mimo-v2.5-pro":               frozenset({Modality.TEXT, Modality.TOOL_CALLING}),
}

_OPENROUTER_DEFAULT_CAPS = frozenset({Modality.TEXT, Modality.TOOL_CALLING})


def _openrouter_capabilities(model_id: str) -> frozenset[Modality]:
    """Return declared modalities for an OpenRouter model id."""
    if not model_id:
        return _OPENROUTER_DEFAULT_CAPS
    return _OPENROUTER_CAPS.get(model_id, _OPENROUTER_DEFAULT_CAPS)
