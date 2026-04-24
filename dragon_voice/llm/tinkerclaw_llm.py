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
from typing import AsyncIterator, Optional

import aiohttp

from dragon_voice.config import LLMConfig
from dragon_voice.llm.base import LLMBackend

logger = logging.getLogger(__name__)

# Wave 14 W14-M07: per-line + total-stream caps for the TinkerClaw SSE
# parser.  The gateway is trusted in theory, but a bug on the other
# end (or a hostile proxy) could stream an unbounded SSE line or
# infinite body that exhausts Dragon's memory budget.  We cap both.
_SSE_MAX_LINE_BYTES = 256 * 1024          # 256 KiB — one SSE "data: ..." frame
_SSE_MAX_STREAM_BYTES = 16 * 1024 * 1024  # 16 MiB — whole response total
_SSE_MAX_TOKENS = 50_000                  # ~200 KB of text, generous

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
                total_bytes = 0  # W14-M07: running total of SSE body bytes
                consecutive_errors = 0  # P11: detect HTML error pages from proxy
                # #B1 TinkerTab audit 2026-04-24: buffer the whole response
                # so we can post-process chain-of-thought leakage before
                # emitting it to the voice pipeline.  We lose token-level
                # streaming for TC mode, but gain the ability to strip the
                # "Let me try another approach:" preamble that TC agents
                # emit when tools fail mid-loop.  For well-behaved replies
                # this is a near no-op (sanitize is idempotent on clean
                # text).  See sanitize_tinkerclaw_reply above.
                accumulated: list[str] = []

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
                    token = delta.get("content", "")
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
                        # #B1: buffer instead of yielding directly so we can
                        # strip the "Let me try another approach:" preamble
                        # once the full reply is in hand.
                        accumulated.append(token)

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

    @property
    def name(self) -> str:
        return f"TinkerClaw ({self._model})"
