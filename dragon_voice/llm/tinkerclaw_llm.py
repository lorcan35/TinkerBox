"""TinkerClaw gateway LLM backend.

Routes LLM inference to the local TinkerClaw agent gateway via its
OpenAI-compatible /v1/chat/completions endpoint. TinkerClaw maintains
its own conversation state, skills, and memory — Dragon just pipes
audio and streams text.
"""

import asyncio
import json
import logging
import re
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

import aiohttp

from dragon_voice.config import LLMConfig
from dragon_voice.errors import DragonError, Scope, Severity
from dragon_voice.llm.base import LLMBackend, Modality

logger = logging.getLogger(__name__)

# γ2-M6 (issue #106) — fast-fail health check budget.
#
# HEALTH_CHECK_TIMEOUT_S = how long a single /health probe is allowed
# to block.  5 s is well under Tab5's PONG-watch (~30 s) so a probe
# during a slow-startup-recovery scenario can't itself trip the
# eviction race.
#
# HEALTH_CACHE_TTL_S = how long a probe result is reused before re-
# probing.  30 s is short enough that recovery after the gateway
# comes back is felt within a turn or two, but long enough that the
# happy path doesn't pay 5 s on every single user turn.
HEALTH_CHECK_TIMEOUT_S = 5
HEALTH_CACHE_TTL_S = 30

# Wave 14 W14-M07: per-line + total-stream caps for the TinkerClaw SSE
# parser.  The gateway is trusted in theory, but a bug on the other
# end (or a hostile proxy) could stream an unbounded SSE line or
# infinite body that exhausts Dragon's memory budget.  We cap both.
_SSE_MAX_LINE_BYTES = 256 * 1024          # 256 KiB — one SSE "data: ..." frame
_SSE_MAX_STREAM_BYTES = 16 * 1024 * 1024  # 16 MiB — whole response total
_SSE_MAX_TOKENS = 50_000                  # ~200 KB of text, generous

# Audit C7 (#137): pre-stream max_tokens cap.  The TC gateway used to
# receive payloads with no max_tokens, letting MiniMax (or whichever
# upstream model TC routed to) generate unbounded — Dragon's
# server-side _SSE_MAX_TOKENS abort would only fire after we'd
# already paid for the over-generated tokens.  Sending a sane cap in
# the payload tells the upstream to stop early.  4096 is a generous
# voice-reply budget (~16 KB of text) while still well under the
# server-side abort threshold.
_TC_REPLY_MAX_TOKENS = 4096

# #B1 (TinkerTab audit 2026-04-24): chain-of-thought / tool-loop preamble
# that TinkerClaw agents routinely leak into the user-facing reply when a
# tool fails ("That didn't work well. Let me try another approach:…Let me
# try a simpler web search:…").  These phrases are the agent's internal
# reasoning between tool calls — on a successful turn they're usually
# followed by a real final answer, but when tools fail outright they ARE
# the whole response and land in the chat bubble as if they were the
# answer.  See `sanitize_tinkerclaw_reply` below.
_COT_PREAMBLE_PATTERNS = [
    # "That didn't work (well)."
    re.compile(r"^(that\s+(?:didn't|did\s+not)\s+work(?:\s+well)?[\.!]?\s*)", re.IGNORECASE),
    # "Let me try another approach/way/search/path:"
    re.compile(r"^(let\s+me\s+try\s+(?:another|a\s+(?:simpler|different))\s+(?:approach|way|search|path)[:\.,]?\s*)", re.IGNORECASE),
    # "Let me try/check/look/search/fetch/see …<60 chars>…:"
    re.compile(r"^(let\s+me\s+(?:try|check|look|search|fetch|see)[^.\n]{0,80}[:\.]\s*)", re.IGNORECASE),
    # "Browser/Search/Fetch (is/'s) (currently)? down/failing/not working[ …]."
    re.compile(
        r"^((?:browser|search|web\s+search|fetch)(?:'s|\s+is)?\s*(?:currently\s+)?"
        r"(?:down|failing|not\s+working|unavailable|timing\s+out)[^.\n]*[\.!]?\s*)",
        re.IGNORECASE,
    ),
    # "Browser's down but I can …:"   <-- leading into CoT fallback
    re.compile(
        r"^((?:browser|search|fetch)(?:'s|\s+is)?\s+(?:down|failing)\s+but\s+i\s+can[^.\n]{0,80}[:\.]\s*)",
        re.IGNORECASE,
    ),
    # "Hmm/OK/Okay/Right/Alright, …"
    re.compile(r"^((?:hmm|ok|okay|right|alright|well)[,.]?\s+)", re.IGNORECASE),
]


# #334 (vmode=3 chip-dark): the OpenClaw gateway's /v1/chat/completions
# SSE stream emits only text deltas — never the OpenAI `tool_calls` delta
# shape — even when the embedded Pi agent does invoke tools (web_search,
# weather, browser, etc.).  As a result Dragon's W7-A delta-accumulator
# path at L498-519 below never fires, and Tab5's agent_log chips stay
# dark in TC mode.
#
# Workaround: scan the assistant's streamed text for narration phrases
# the agent reliably emits when it actually used a tool ("Based on my
# weather skill, let me fetch live data for Geneva: ..."), match the
# captured token against a known-tool allowlist, and synthesize a
# tool_call event through the existing `_on_tool_call` callback.  The
# heuristic is gated by the allowlist so plain-language phrases like
# "based on my knowledge" don't false-fire.
_TC_TEXT_TOOL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "Based on my weather skill", "Using the browser tool", "Found the
    # weather skill", "Let me use my web_search extension", "fetched
    # from my memory skill", "Ran the web_search tool"
    re.compile(
        r"\b(?:based\s+on|using|with|fetched?\s+(?:from|via)|found|loaded|"
        r"invoked?|called|ran|executed|"
        r"let\s+me\s+(?:use|check\s+with|query|fetch\s+from))\s+"
        r"(?:my\s+|the\s+|its\s+)?"
        r"([a-z][a-z0-9_\- ]{1,30}?)"
        r"\s+(?:tool|skill|extension|service)\b",
        re.IGNORECASE,
    ),
    # "searching the web for ...", "fetching weather data", "looking up
    # the date", "querying memory" — verb + known-subject form
    re.compile(
        r"\b(?:searching|querying|fetching|looking\s+up|checking|invoking)\s+"
        r"(?:the\s+|my\s+|via\s+)?"
        r"\b(web(?:\s+search)?|google|wikipedia|memory|weather|email|"
        r"telegram|whatsapp|discord|slack|signal|browser|calendar|news|"
        r"youtube|gmail|stock\s+ticker|date(?:time)?)\b",
        re.IGNORECASE,
    ),
    # Skill-path references — OpenClaw agents commonly cite their own
    # skill manifests in narration, like
    # "~/tinkerclaw/skills/weather/SKILL.md".  The path segment between
    # `skills/` and the next `/` is the canonical skill name and a much
    # higher-precision signal than free-text "X skill" phrasing.
    re.compile(
        r"(?:tinkerclaw|\.tinkerclaw|openclaw)/skills?/([a-z][a-z0-9_-]{1,30})/",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bskills?/([a-z][a-z0-9_-]{1,30})/SKILL\.md\b",
        re.IGNORECASE,
    ),
)

# Allowlist of tool names that the synthetic tool_call surface is
# willing to emit.  Heuristic matches whose normalized name is NOT in
# this set are silently dropped so phrases like "based on my knowledge"
# / "let me try a different approach" don't show up as tool calls.
#
# Names are normalized via `_normalize_tool_name` before lookup
# (lowercase, spaces → underscores, hyphens → underscores).  The set
# mirrors the merged catalog the W7-B `/api/v1/agent_skills` endpoint
# returns: 8 OpenClaw core tools + the Dragon ToolRegistry built-ins +
# the common channel plugin names (so Telegram/WhatsApp/etc. narration
# surfaces a chip too).
_TC_KNOWN_TOOLS: frozenset[str] = frozenset({
    # OpenClaw core (per W7-B static catalog)
    "bash", "browser", "edit_file", "memory", "read_file",
    "search_files", "task", "web_search",
    # Dragon ToolRegistry built-ins
    "web", "search", "weather", "calculator", "unit_converter",
    "stock_ticker", "datetime", "date", "remember", "recall",
    "forget", "note", "system", "timesense", "quick_poll",
    # Channel plugins (so "fetching from telegram" → telegram chip)
    "telegram", "whatsapp", "discord", "slack", "signal",
    "imessage", "matrix", "email", "gmail",
    # Common skill family aliases
    "google", "wikipedia", "calendar", "news", "youtube",
})


def _normalize_tool_name(raw: str) -> str:
    """Fold a regex-captured tool name to the allowlist's canonical form."""
    out = (raw or "").strip().lower()
    # "stock ticker" → "stock_ticker"; "web search" → "web_search"
    out = re.sub(r"\s+", "_", out)
    # "datetime" / "date" both map to "datetime" in the allowlist
    if out == "date":
        out = "datetime"
    return out


def extract_inferred_tool_invocations(
    text: str, already_fired: frozenset[str] | set[str] = frozenset(),
) -> list[str]:
    """Return canonical tool names inferred from agent narration.

    Scans `text` for phrases the OpenClaw embedded agent reliably emits
    when it uses a tool, maps captured names through the
    `_TC_KNOWN_TOOLS` allowlist, and returns each new name in match
    order.  Names already in `already_fired` are skipped so the caller
    can stream-scan an accumulator without re-emitting earlier matches.

    Public (module-level rather than `_`-prefixed) so the unit test
    suite at `tests/test_tinkerclaw_text_tool_inference.py` can drive
    it directly without instantiating a backend.
    """
    if not text:
        return []
    seen_this_pass: set[str] = set()
    out: list[str] = []
    for pat in _TC_TEXT_TOOL_PATTERNS:
        for match in pat.finditer(text):
            raw = match.group(1)
            norm = _normalize_tool_name(raw)
            if norm not in _TC_KNOWN_TOOLS:
                continue
            if norm in already_fired or norm in seen_this_pass:
                continue
            seen_this_pass.add(norm)
            out.append(norm)
    return out


def sanitize_tinkerclaw_reply(text: str) -> str:
    """Strip leading chain-of-thought / tool-loop preamble from a TC reply.

    Runs once on the full accumulated response.  Repeatedly peels known
    "Let me try / That didn't work / Browser's down" preamble sentences
    off the front until none match.  If NOTHING remains after peeling —
    meaning the whole response was agent self-talk with no final answer
    — returns a generic "couldn't complete that" message so the user
    doesn't see raw reasoning in the chat bubble.
    """
    if not text:
        return text
    work = text.lstrip()
    # Bound the stripping loop so a pathological pattern match can't
    # spin forever.
    for _ in range(20):
        before = work
        for pat in _COT_PREAMBLE_PATTERNS:
            work = pat.sub("", work, count=1).lstrip()
        if work == before:
            break
    if not work.strip():
        return "I couldn't complete that — a tool I needed came back empty. Want to try a different way?"
    return work


def _classify_gateway_source(tool_name: str) -> str:
    """W7-G: bucket gateway-routed tool calls so /api/v1/agent_log can
    surface browser activity separately from generic gateway work.

    OpenClaw exposes `browser` as its web-control tool (see
    `openclaw/src/agents/tool-catalog.ts`), and the wider ecosystem
    has variants (`browser_actions`, `browser.click`, etc.).  Treat
    all of them as one bucket — `gateway_browser` — so operators see
    a distinct counter for "the agent is on the web right now" vs
    "the agent ran a shell command or RAG lookup."  Everything else
    stays at the legacy `gateway` source.
    """
    name = (tool_name or "").strip().lower()
    if not name:
        return "gateway"
    if name == "browser" or name.startswith("browser_") or name.startswith("browser."):
        return "gateway_browser"
    return "gateway"


class TinkerClawBackend(LLMBackend):
    """LLM backend that proxies to a local TinkerClaw agent gateway."""

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._url = (config.tinkerclaw_url or "http://localhost:18789").rstrip("/")
        self._token = (config.tinkerclaw_token or "").strip()
        # Wave 13 H6: fail fast at construction rather than silently sending
        # unauthenticated requests that the gateway will reject with 401 on
        # every single turn. The misconfigured case used to look like a
        # "model is dumb" bug from the user's perspective.
        if not self._token:
            raise ValueError(
                "tinkerclaw_token is blank — set TINKERCLAW_TOKEN in env "
                "or llm.tinkerclaw_token in config.yaml. Gateway (port "
                "18789) rejects every request without a bearer token."
            )
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
        # γ2-M6 (issue #106): cached health-check state.
        # _health_cache_until=0.0 means "no cached result, must probe
        # on next is_healthy() call".  Both fields are set together
        # by is_healthy() and seeded by initialize().
        self._health_cache_ok: bool = False
        self._health_cache_until: float = 0.0
        # W7-A (audit 2026-05-11): optional callback fired once per
        # full tool call detected in the gateway's SSE stream.  When
        # set, the SSE parser surfaces `delta.tool_calls` deltas from
        # the OpenAI-compatible /v1/chat/completions response as
        # complete tool_call events — Tab5 already renders these
        # (Wave 12 agent_log feed).  Pre-W7-A the deltas were silently
        # skipped because the parser only looked at `delta.content`.
        self._on_tool_call: Optional[Callable[[dict], Awaitable[None]]] = None
        # W7-A.2: companion callback fired when we synthesize a
        # tool_result event.  /v1/chat/completions doesn't emit
        # tool_result natively, so we infer "tool completed" from the
        # SSE pattern: after a tool_call emits, the next assistant
        # content delta in the same stream is the user-visible answer
        # — meaning the tool has finished.  We then emit one
        # tool_result per pending tool_call so Tab5's UI can flip the
        # spinning indicator to "done" instead of leaving it spinning
        # forever.  Result payload is intentionally minimal — the
        # agent's text reply IS the user-visible answer; this event
        # only carries the "completed" signal.
        self._on_tool_result: Optional[Callable[[dict], Awaitable[None]]] = None

    async def initialize(self) -> None:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        self._session = aiohttp.ClientSession(
            # 2026-04-23 (#58): bumped total 180→600 and sock_read 90→600.
            # Previous 90 s budget killed TC agents mid-work — any skill that
            # authored a file, cloned a repo, or did multi-step planning
            # routinely exceeded 90 s of silent streaming.  User saw
            # "Response timed out, please try again." on Tab5 while the
            # agent was actively modifying files on Dragon.  10 min covers
            # real TC agent workloads with headroom.
            timeout=aiohttp.ClientTimeout(total=600, sock_read=600),
            headers=headers,
        )

        # γ2-M6 (issue #106): seed the health cache so the first request
        # after boot is fast — no extra 5 s probe just because we forgot
        # we already checked at init.  Force=True bypasses the empty
        # cache check and writes the result regardless.
        await self.is_healthy(force=True)

    async def is_healthy(self, force: bool = False) -> bool:
        """Probe the TC gateway's /health endpoint with caching.

        γ2-M6 (issue #106) — the audit identified that when the gateway
        is reachable at TCP layer but hung / dead at app layer, the
        first POST blocks for the full 600 s sock_read window.  This
        method gives ``generate_stream_with_messages`` a 5 s yes/no
        answer with 30 s caching so the user doesn't wait 10 minutes
        before the connection-error fallback fires.

        Parameters
        ----------
        force:
            If True, bypass the cache and always re-probe.  Use for
            ops debugging (manual recovery check after restarting the
            gateway) — production callers leave it False.

        Returns
        -------
        bool
            True if /health returned 200 within HEALTH_CHECK_TIMEOUT_S,
            False otherwise (any non-200, ClientError, TimeoutError).
            Never raises.
        """
        now = time.monotonic()
        if not force and now < self._health_cache_until:
            return self._health_cache_ok

        if self._session is None or self._session.closed:
            # No live session means we haven't initialised yet.
            self._health_cache_ok = False
            self._health_cache_until = now + HEALTH_CACHE_TTL_S
            return False

        ok = False
        try:
            async with self._session.get(
                f"{self._url}/health",
                timeout=aiohttp.ClientTimeout(total=HEALTH_CHECK_TIMEOUT_S),
            ) as resp:
                ok = resp.status == 200
                if not ok:
                    logger.warning(
                        "TinkerClaw health check returned %d", resp.status
                    )
                else:
                    logger.debug("TinkerClaw health check OK")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning(
                "TinkerClaw health check failed: %s (cached for %ds)",
                e, HEALTH_CACHE_TTL_S,
            )
        # Cache regardless of outcome — both healthy and unhealthy
        # results get the TTL so we don't spam-probe a down gateway.
        self._health_cache_ok = ok
        self._health_cache_until = now + HEALTH_CACHE_TTL_S
        return ok

    async def health_check(self, timeout_s: float = 2.0) -> tuple[bool, str]:
        """W4-B: surface the cached gateway-health probe to `/health`.

        Reuses the existing `is_healthy()` 30 s cache so a /health hit
        every few seconds doesn't re-probe the gateway each time — the
        operator gets a near-realtime answer with stable load.
        `timeout_s` is informational here (the underlying probe budget
        is `HEALTH_CHECK_TIMEOUT_S=5`); we honor `timeout_s` only for
        the cache-lookup path (which is sub-ms anyway).
        """
        try:
            ok = await asyncio.wait_for(
                self.is_healthy(force=False),
                timeout=max(timeout_s, HEALTH_CHECK_TIMEOUT_S + 0.5),
            )
        except asyncio.TimeoutError:
            return False, f"timeout after {timeout_s}s"
        except Exception as e:  # noqa: BLE001 — must never raise
            return False, f"{type(e).__name__}: {e}"[:120]
        return (True, f"{self._url}") if ok else (False, "gateway /health not 200")

    def set_session_key(self, session_key: str) -> None:
        """Set the session key for conversation continuity.

        Called by pipeline/server before each request. TinkerClaw uses this
        to maintain per-session conversation history internally.
        """
        self._session_key = session_key

    def set_tool_event_handler(
        self,
        on_tool_call: Optional[Callable[[dict], Awaitable[None]]],
        on_tool_result: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> None:
        """W7-A / W7-A.2: register the gateway tool-event callbacks.

        `on_tool_call(payload)` is invoked once per fully-assembled
        tool call detected in the SSE stream.  Payload shape matches
        Dragon's own ToolRegistry emit:
            {"tool": "<name>", "args": <parsed dict>}
        so Tab5's existing Wave 12 agent_log feed renders it unchanged.

        `on_tool_result(payload)` is invoked once per **synthesized**
        completion — when the SSE stream emits an assistant content
        delta AFTER a pending tool_call, we infer the tool finished
        and fire a result event so Tab5's chat UI can flip the
        spinning indicator to "done."  Payload shape:
            {"tool": "<name>", "result": None, "execution_ms": None}
        The agent's text reply IS the user-visible answer; this
        callback only carries the "completed" signal, since
        /v1/chat/completions doesn't expose the tool's actual return
        value to the client.

        Pass `None` to clear either handler (e.g. on backend swap).

        Failure modes for both callbacks:
          * raises an exception → logged + swallowed (a buggy callback
            must NOT tear down the LLM stream)
          * blocks for more than the sock_read budget → can stall the
            stream; keep callbacks fast (the existing ToolEventEmitter
            already does fire-and-forget WS sends).
        """
        self._on_tool_call = on_tool_call
        self._on_tool_result = on_tool_result

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

        # γ2-M6 (issue #106): fast-fail before the 600 s POST when the
        # gateway is known to be down.  is_healthy() uses a 30 s cached
        # result so the happy path doesn't pay for a probe per turn;
        # only the first turn after the gateway goes down (or after the
        # cache expires) waits the 5 s probe budget.
        if not await self.is_healthy():
            raise DragonError(
                "TinkerClaw agent gateway isn't responding — "
                "fall back to local mode and try again.",
                code="gateway_unreachable",
                severity=Severity.FATAL,
                scope=Scope.GATEWAY,
            )

        payload = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            # Audit C7 (#137): pre-stream cap so the upstream model
            # doesn't generate beyond what we'll accept (and bill).
            "max_tokens": _TC_REPLY_MAX_TOKENS,
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
                total_bytes = 0  # W14-M07: running total of SSE body bytes
                consecutive_errors = 0  # P11: detect HTML error pages from proxy
                # Audit A5 (#144): two-phase streaming.  We still need to
                # peel CoT preamble that TC agents leak between tool calls
                # ("Let me try another approach:..."), but we no longer
                # buffer the WHOLE reply.  Instead we buffer just enough
                # of the front to either (a) prove no preamble matched and
                # safely passthrough, or (b) fully cover the longest known
                # preamble pattern.  All `_COT_PREAMBLE_PATTERNS` are
                # `^`-anchored, so they only ever peel from the front —
                # once we've passed the preamble window the residue is
                # identical to passthrough, sanitiser-wise.
                accumulated: list[str] = []
                passthrough = False
                # Tunables: chosen so MiniMax-style happy replies (no
                # preamble, ~10 tok/s, ~5 ch/tok) hit passthrough at
                # roughly the first sentence end ≈ 1-2 s after the first
                # token, while a long preamble has up to ~400 chars to
                # finish before the safety cap forces a flush.
                _A5_EARLY_CHECK_MIN_CHARS = 60
                _A5_SAFETY_FLUSH_CHARS = 400
                _A5_PARAGRAPH_BREAK = "\n\n"
                _A5_SENTENCE_END_CHARS = (".", "!", "?")

                # W7-A: tool-call accumulator keyed by OpenAI's delta
                # `tool_calls[*].index`.  OpenAI streams function name +
                # arguments incrementally; we buffer per-index and flush
                # when the index closes (next-chunk's tool_calls has a
                # different index, or [DONE]/text-delta indicates calls
                # are complete).  Flushed entries fire `_on_tool_call`.
                tool_call_buffer: dict[int, dict[str, Any]] = {}
                _flushed_tool_indices: set[int] = set()
                # W7-A.2: pending tool calls awaiting a synthesized
                # result.  Populated by _flush_tool_calls; drained by
                # _emit_synthetic_results when the next assistant
                # content burst arrives (the natural "tool finished"
                # boundary on /v1/chat/completions).
                _pending_results: list[str] = []

                # #334: text-narration tool inference.  The gateway never
                # emits structured tool_calls deltas, so we rebuild a
                # surface for Tab5's W7-A chips by scanning the streamed
                # assistant text for known skill-invocation phrases.
                # `_text_scan_buf` accumulates ALL streamed text
                # regardless of the passthrough/buffer state above; the
                # scanner runs at sentence boundaries to keep per-token
                # cost low.  `_text_inferred_tools` is the dedupe set —
                # each tool name fires at most once per turn.
                _text_scan_buf: list[str] = []
                _text_inferred_tools: set[str] = set()

                # W14-M07: use readline with explicit byte cap so one
                # pathologically long SSE frame can't blow memory.
                while True:
                    raw = await resp.content.readline()
                    if not raw:
                        break
                    if len(raw) > _SSE_MAX_LINE_BYTES:
                        logger.error(
                            "M07: SSE line exceeded %d bytes — aborting stream",
                            _SSE_MAX_LINE_BYTES,
                        )
                        yield " ... (response aborted: oversized SSE frame)"
                        return
                    total_bytes += len(raw)
                    if total_bytes > _SSE_MAX_STREAM_BYTES:
                        logger.error(
                            "M07: SSE stream exceeded %d bytes total — aborting",
                            _SSE_MAX_STREAM_BYTES,
                        )
                        yield " ... (response aborted: stream too large)"
                        return
                    line = raw.decode("utf-8", errors="replace").strip()
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

                    # W7-A: accumulate any incremental tool_calls deltas
                    # by index before reading the text-content delta.
                    # OpenAI's chat-completions streams tool_calls
                    # incrementally — `function.name` arrives in one
                    # chunk, `function.arguments` is split character by
                    # character across many chunks.  We rebuild each
                    # call here; the actual flush happens when we see
                    # a finish_reason of "tool_calls" / "stop" or when
                    # the stream ends.
                    self._accumulate_tool_call_deltas(
                        delta.get("tool_calls"), tool_call_buffer,
                    )

                    # If the upstream signals "tool_calls finished",
                    # flush buffered calls immediately so Tab5 sees
                    # them before the tool execution latency starts.
                    finish_reason = choices[0].get("finish_reason")
                    if finish_reason in ("tool_calls", "stop") and tool_call_buffer:
                        await self._flush_tool_calls(
                            tool_call_buffer, _flushed_tool_indices,
                            pending_results=_pending_results,
                        )

                    token = delta.get("content", "")
                    # W7-A.2: first content burst after pending tool
                    # calls means those tools finished.  Fire one
                    # synthesized tool_result event per pending tool.
                    if token and _pending_results:
                        await self._emit_synthetic_results(_pending_results)

                    # #334: scan streamed text for tool-narration markers
                    # ("based on my weather skill", "searching the web",
                    # etc.) and emit synthesized tool_call events through
                    # the same `_on_tool_call` pipeline W7-A uses for
                    # real deltas.  Throttled to sentence boundaries to
                    # keep per-token cost negligible.
                    if token:
                        _text_scan_buf.append(token)
                        if any(c in token for c in (".", "!", "?", "\n", ":")):
                            await self._scan_text_for_inferred_tools(
                                "".join(_text_scan_buf),
                                already_fired=_text_inferred_tools,
                                pending_results=_pending_results,
                            )
                    if token:
                        token_count += 1
                        if token_count > _SSE_MAX_TOKENS:
                            logger.error(
                                "M07: SSE token count exceeded %d — aborting",
                                _SSE_MAX_TOKENS,
                            )
                            # Flush whatever we buffered so the user sees
                            # *something* before the abort notice.
                            if accumulated:
                                yield sanitize_tinkerclaw_reply("".join(accumulated))
                                accumulated.clear()
                            yield " ... (response aborted: too many tokens)"
                            return
                        # Audit A5 (#144): once the preamble window has
                        # passed, stream tokens directly so the user
                        # doesn't wait 30-60 s for a full reply to land.
                        if passthrough:
                            yield token
                            continue
                        accumulated.append(token)
                        joined = "".join(accumulated)
                        # Hard safety: any preamble longer than this is
                        # pathological — flush sanitised, switch to
                        # passthrough so the rest of the reply still
                        # streams instead of waiting on more buffering.
                        if len(joined) >= _A5_SAFETY_FLUSH_CHARS:
                            cleaned = sanitize_tinkerclaw_reply(joined)
                            if cleaned:
                                yield cleaned
                            accumulated.clear()
                            passthrough = True
                            continue
                        # Paragraph break is a strong "preamble is over"
                        # signal — TC agents put a blank line between
                        # internal reasoning and the final answer when
                        # they bother to format it.
                        if _A5_PARAGRAPH_BREAK in joined:
                            cleaned = sanitize_tinkerclaw_reply(joined)
                            if cleaned:
                                yield cleaned
                            accumulated.clear()
                            passthrough = True
                            continue
                        # Early flush: at any sentence boundary past the
                        # check threshold, run sanitise.  If the cleaned
                        # residue carries at least ~20 chars of real
                        # content, the answer has started — flush the
                        # cleaned prefix (preamble peeled or not) and
                        # passthrough the remainder so the user sees
                        # streaming instead of a 30-60 s freeze.  Only
                        # check at sentence ends to keep the per-token
                        # cost low.
                        if (
                            len(joined) >= _A5_EARLY_CHECK_MIN_CHARS
                            and any(c in token for c in _A5_SENTENCE_END_CHARS)
                        ):
                            cleaned = sanitize_tinkerclaw_reply(joined)
                            if len(cleaned.strip()) >= 20:
                                yield cleaned
                                accumulated.clear()
                                passthrough = True

                # #334: final scan of the full accumulated text — catch
                # narration that landed in the last chunk without a
                # sentence-ending punctuation.
                if _text_scan_buf:
                    await self._scan_text_for_inferred_tools(
                        "".join(_text_scan_buf),
                        already_fired=_text_inferred_tools,
                        pending_results=_pending_results,
                    )

                # W7-A: end-of-stream flush — any tool calls that the
                # upstream never gave a finish_reason for (some servers
                # only emit finish_reason on the assistant-text path, not
                # on tool-only chunks) still need to surface to Tab5.
                if tool_call_buffer:
                    await self._flush_tool_calls(
                        tool_call_buffer, _flushed_tool_indices,
                        pending_results=_pending_results,
                    )
                # W7-A.2: any pending tool calls without a subsequent
                # content burst still completed (the stream ended).
                # Fire results so Tab5's UI doesn't strand the spinner.
                if _pending_results:
                    await self._emit_synthetic_results(_pending_results)

                # A07: Truncation detection — stream ended without [DONE]
                if not saw_done:
                    if token_count > 0:
                        logger.warning(
                            "SSE stream ended without [DONE] after %d tokens "
                            "— response may be truncated (TinkerClaw crash?)",
                            token_count,
                        )
                        if accumulated:
                            yield sanitize_tinkerclaw_reply("".join(accumulated))
                            accumulated.clear()
                        yield " ... (response interrupted)"
                    else:
                        logger.warning(
                            "SSE stream ended without [DONE] and 0 tokens "
                            "— connection may have dropped before any response"
                        )
                        yield "I'm having trouble connecting to my agent system. Please try again in a moment."
                else:
                    # #B1 happy path: clean and yield the full reply.
                    if accumulated:
                        yield sanitize_tinkerclaw_reply("".join(accumulated))
                        accumulated.clear()

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

    # ── W7-A: gateway tool-event surfacing ─────────────────────────────

    @staticmethod
    def _accumulate_tool_call_deltas(
        deltas: Optional[list[dict]],
        buffer: dict[int, dict[str, Any]],
    ) -> None:
        """Merge incremental OpenAI tool_calls deltas into the buffer.

        OpenAI streams tool calls in pieces — a chunk may carry just
        `{"index": 0, "id": "call_abc"}`, the next `{"index": 0,
        "function": {"name": "search"}}`, then a long tail of
        `{"index": 0, "function": {"arguments": "{\"q\""}}` etc.  We
        rebuild the full call in `buffer[index]`.

        No callback is fired here — flush is the caller's job, since
        the right moment depends on context (finish_reason / EOS).
        """
        if not deltas:
            return
        for d in deltas:
            try:
                idx = int(d.get("index", 0))
            except (TypeError, ValueError):
                idx = 0
            slot = buffer.setdefault(
                idx, {"id": "", "name": "", "arguments": ""},
            )
            call_id = d.get("id")
            if call_id:
                slot["id"] = str(call_id)
            fn = d.get("function") or {}
            name = fn.get("name")
            if name:
                # Some servers stream name in fragments too; concat
                # rather than replace to be defensive.
                slot["name"] += str(name) if not slot["name"] else ""
                if not slot["name"]:
                    slot["name"] = str(name)
            arg_frag = fn.get("arguments")
            if arg_frag:
                slot["arguments"] += str(arg_frag)

    async def _flush_tool_calls(
        self,
        buffer: dict[int, dict[str, Any]],
        flushed: set[int],
        pending_results: Optional[list[str]] = None,
    ) -> None:
        """Emit accumulated tool calls via the registered callback +
        record into the cross-session agent_log ring (Wave 12 surface).

        Each flushed tool name is appended to `pending_results` (if
        provided) so W7-A.2's synthetic tool_result emitter knows which
        tools still need a "completed" signal sent when the next
        assistant content burst arrives.

        Each buffer entry is rendered as Dragon's standard tool_call
        shape (`{"tool": "<name>", "args": <parsed dict>}`) and passed
        to `self._on_tool_call`.  Failures are logged + swallowed so a
        buggy callback can never tear down the LLM stream.

        After the WS-side emit, the same call is appended to the
        agent_log ring buffer (`dragon_voice.api.agent_log.record_call`)
        so the `/api/v1/agent_log` feed surfaces gateway-routed calls
        alongside the local-ToolRegistry ones — without the gateway
        having to expose a separate skill-discovery endpoint.  This
        closes the Wave 12 visibility gap mode 3 had:  ToolRegistry's
        own chokepoint is bypassed when the LLM is `tinkerclaw`, so
        agent_log was previously dark for mode-3 sessions.

        Idempotent: indexes already in `flushed` are skipped, so we can
        be called at finish_reason and again at EOS without duplicate
        emits.  The buffer itself is not cleared — keeping the records
        lets future runs in the same stream (rare) accumulate further
        argument fragments correctly.
        """
        for idx, call in buffer.items():
            if idx in flushed:
                continue
            name = call.get("name", "").strip()
            if not name:
                # Tool calls with no function name are gateway bugs;
                # surface nothing rather than emit a malformed event.
                continue
            args_str = call.get("arguments") or "{}"
            try:
                args_obj: Any = json.loads(args_str) if args_str else {}
            except json.JSONDecodeError:
                # Streaming may have ended mid-JSON if the upstream
                # crashed; surface the raw string so the obs ring shows
                # *something* useful for debugging.
                args_obj = {"_raw": args_str[:512]}
            payload = {"tool": name, "args": args_obj}
            if self._on_tool_call is not None:
                try:
                    await self._on_tool_call(payload)
                except Exception:
                    logger.exception(
                        "W7-A: on_tool_call callback raised — swallowed "
                        "to keep LLM stream alive (tool=%s)",
                        name,
                    )
            # W7-A.b: record into the agent_log ring so /api/v1/agent_log
            # surfaces gateway-routed calls.  Lazy-import to keep this
            # module standalone if the API layer is ever broken out.
            # Failures are best-effort — the ring buffer should never
            # tear down LLM streaming.
            try:
                from dragon_voice.api.agent_log import record_call as _alog
                _alog(
                    name,
                    args_obj if isinstance(args_obj, dict) else {},
                    source=_classify_gateway_source(name),
                )
            except Exception:
                logger.debug(
                    "agent_log record_call suppressed for gateway tool %s",
                    name, exc_info=True,
                )
            if pending_results is not None:
                pending_results.append(name)
            flushed.add(idx)

    async def _emit_synthetic_results(self, pending: list[str]) -> None:
        """W7-A.2: emit a synthetic tool_result per pending tool name.

        `/v1/chat/completions` doesn't carry the tool's actual return
        value to the client (it executes server-side and feeds the
        text reply directly).  The user-visible answer arrives in the
        next content burst, which is also the natural "tool finished"
        boundary.  We fire one tool_result per pending tool so Tab5's
        UI can flip the spinner to "done" — the result payload is
        minimal (no actual data) because the agent's prose answer IS
        the data the user wanted.

        Also feeds the cross-session agent_log ring so
        `/api/v1/agent_log` flips each call's status from "running"
        to "done" (matches Wave 12 contract).

        Idempotent: `pending` is drained as we emit, so a second call
        is a no-op.  Empty list → no-op.  Failures in either the
        WS callback or the agent_log write are best-effort.
        """
        # Drain to a local copy so a callback that re-enters the
        # parser can't double-emit.
        names = list(pending)
        pending.clear()
        if not names:
            return
        for name in names:
            payload = {"tool": name, "result": None, "execution_ms": None}
            if self._on_tool_result is not None:
                try:
                    await self._on_tool_result(payload)
                except Exception:
                    logger.exception(
                        "W7-A.2: on_tool_result callback raised — swallowed "
                        "to keep LLM stream alive (tool=%s)",
                        name,
                    )
            try:
                from dragon_voice.api.agent_log import record_result as _alog_res
                _alog_res(
                    name,
                    result=None,
                    execution_ms=None,
                    source=_classify_gateway_source(name),
                )
            except Exception:
                logger.debug(
                    "agent_log record_result suppressed for gateway tool %s",
                    name, exc_info=True,
                )

    async def _scan_text_for_inferred_tools(
        self,
        buffered_text: str,
        already_fired: set[str],
        pending_results: list[str],
    ) -> None:
        """#334: emit synthetic `tool_call` events from narration text.

        Runs `extract_inferred_tool_invocations` against the accumulated
        assistant text, fires `_on_tool_call` for each new name, and
        registers the name in `pending_results` so the existing W7-A.2
        synthetic-result machinery flips the chip to "done" on the next
        content burst (or at end of stream).

        Callback failures are logged + swallowed so a misbehaving
        downstream consumer never tears down the LLM stream.
        """
        inferred = extract_inferred_tool_invocations(
            buffered_text, already_fired=already_fired,
        )
        if inferred:
            logger.debug(
                "#334: inferred tools from narration: %s", inferred,
            )
        for name in inferred:
            already_fired.add(name)
            payload = {"tool": name, "args": {"_inferred": True}}
            if self._on_tool_call is not None:
                try:
                    await self._on_tool_call(payload)
                except Exception:
                    logger.exception(
                        "#334: on_tool_call callback raised for inferred "
                        "tool (tool=%s) — swallowed",
                        name,
                    )
            try:
                from dragon_voice.api.agent_log import record_call as _alog
                _alog(
                    name,
                    {"_inferred": True},
                    source=_classify_gateway_source(name),
                )
            except Exception:
                logger.debug(
                    "agent_log record_call suppressed for inferred tool %s",
                    name, exc_info=True,
                )
            pending_results.append(name)

    @property
    def name(self) -> str:
        return f"TinkerClaw ({self._model})"

    @property
    def capabilities(self) -> frozenset[Modality]:
        """TinkerClaw gateway: text + tools always; +VISION on multimodal upstreams.

        Delegates to the centralized capability registry (#200, Wave 21).
        Declared mostly for completeness + diagnostic surfaces; the
        router doesn't pick TinkerClaw (voice_mode=3 short-circuits
        the router entirely).
        """
        from .capability_registry import detect

        return detect("tinkerclaw", self._model or "")
