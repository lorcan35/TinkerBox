"""Tests for the TinkerClawBackend low-latency streaming path (audit A5).

Pre-fix the backend buffered the entire SSE reply and yielded only at
end-of-stream so `sanitize_tinkerclaw_reply` could peel CoT preamble.
That worked but felt broken: the user saw a 30-60 s thinking indicator
followed by the whole reply landing at once.

Post-fix the backend streams tokens once the preamble window has
passed.  These tests pin both the latency improvement and the
preamble-stripping correctness using a tiny FakeSSE harness so we
don't need a live TC gateway.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Iterable
from unittest.mock import MagicMock

import pytest

from dragon_voice.config import LLMConfig
from dragon_voice.llm.tinkerclaw_llm import TinkerClawBackend


# ─────────────────────────── FakeSSE harness


def _sse_lines_from_token_groups(groups: Iterable[Iterable[str]]) -> list[bytes]:
    """Build a sequence of SSE 'data: {...}' lines that yield each
    string in `groups` as a content delta, terminated with [DONE].

    Each inner iterable becomes a single SSE chunk (so we can simulate
    "first chunk has the whole preamble, then the answer trickles in").
    Real TC frames are one token per line, so we flatten.
    """
    lines: list[bytes] = []
    for group in groups:
        for tok in group:
            payload = {"choices": [{"delta": {"content": tok}}]}
            lines.append(b"data: " + json.dumps(payload).encode() + b"\n")
    lines.append(b"data: [DONE]\n")
    lines.append(b"")  # EOF
    return lines


def _make_backend_with_fake_stream(sse_lines: list[bytes]) -> TinkerClawBackend:
    cfg = LLMConfig(backend="tinkerclaw", tinkerclaw_token="fake-test-token")
    backend = TinkerClawBackend(cfg)
    backend._health_cache_ok = True
    backend._health_cache_until = time.monotonic() + 30.0

    class _FakeReader:
        def __init__(self, lines: list[bytes]) -> None:
            self._lines = list(lines)

        async def readline(self) -> bytes:
            if not self._lines:
                return b""
            return self._lines.pop(0)

    class _FakePostResponse:
        status = 200

        def __init__(self, lines: list[bytes]) -> None:
            self._reader = _FakeReader(lines)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        @property
        def content(self):
            return self._reader

    session = MagicMock()
    session.closed = False
    session.post = MagicMock(return_value=_FakePostResponse(sse_lines))
    backend._session = session
    return backend


async def _drain(backend: TinkerClawBackend) -> list[str]:
    out: list[str] = []
    async for chunk in backend.generate_stream_with_messages(
        [{"role": "user", "content": "hi"}]
    ):
        out.append(chunk)
    return out


# ─────────────────────────── happy path: no preamble → multiple chunks


def test_no_preamble_streams_in_chunks() -> None:
    """A reply with no CoT preamble must NOT be buffered to the end —
    once a sentence-end punctuation appears past the early-check
    threshold, sanitiser is a no-op and the rest streams token-by-token.
    """
    # First chunk together is ~70 chars and ends in "."  → triggers
    # early-flush + passthrough.  Subsequent tokens stream.
    intro = (
        "The capital of France is Paris and it is on the river Seine."
    )
    intro_tokens = list(intro)  # one char per token, exaggerated
    tail_tokens = [" Paris is also home to the Louvre."]
    sse = _sse_lines_from_token_groups([intro_tokens, tail_tokens])
    backend = _make_backend_with_fake_stream(sse)

    out = asyncio.run(_drain(backend))

    # Must produce at least 2 yielded chunks: the buffered prefix +
    # at least one passthrough token.  The test fails if everything
    # comes out as a single end-of-stream yield (i.e. pre-A5 behaviour).
    assert len(out) >= 2, (
        f"expected ≥ 2 chunks (buffered prefix + passthrough), got {len(out)}: {out!r}"
    )
    # And the joined output must equal the original text (no loss).
    assert "".join(out) == intro + tail_tokens[0]


# ─────────────────────────── preamble + real answer


def test_leading_cot_preamble_is_stripped_and_answer_streams() -> None:
    """An LLM that emits 'Let me try a simpler search:' followed by the
    real answer must (a) have the preamble peeled and (b) still stream
    the answer in pieces."""
    preamble = "Let me try a simpler search: "
    answer = "The result is 359784. The calculation is correct."
    answer_tail = " Let me know if you need a different format."

    tokens = list(preamble) + list(answer)  # one char/token
    tail = [answer_tail]
    sse = _sse_lines_from_token_groups([tokens, tail])
    backend = _make_backend_with_fake_stream(sse)

    out = asyncio.run(_drain(backend))
    joined = "".join(out)

    # Preamble peeled.
    assert "Let me try a simpler search:" not in joined, (
        f"preamble leaked into reply: {joined!r}"
    )
    # Answer survived.
    assert "359784" in joined
    # Streamed in pieces (not single end-of-stream blob).
    assert len(out) >= 2, f"expected streamed chunks, got {out!r}"


# ─────────────────────────── all-preamble reply → canned fallback


def test_all_preamble_reply_returns_canned_message() -> None:
    """When the entire SSE body is CoT chatter with no real answer, the
    end-of-stream sanitiser must return the canned 'I couldn't complete
    that' message — never the raw reasoning."""
    # Short enough to never hit the safety cap, no real sentence end
    # outside the preamble pattern set.
    cot = (
        "That didn't work well. "
        "Let me try another approach: "
        "Browser's down."
    )
    sse = _sse_lines_from_token_groups([list(cot)])
    backend = _make_backend_with_fake_stream(sse)

    out = asyncio.run(_drain(backend))
    joined = "".join(out)

    assert "I couldn't complete that" in joined, (
        f"expected canned fallback when all-preamble; got {joined!r}"
    )
    # Original CoT text must NOT leak.
    assert "Let me try another approach" not in joined
    assert "Browser's down" not in joined


# ─────────────────────────── safety cap


def test_long_preamble_above_safety_cap_eventually_flushes() -> None:
    """If the preamble is longer than SAFETY_FLUSH_CHARS (400), the
    backend must flush sanitised content rather than buffer forever."""
    # 600 chars of pseudo-preamble that doesn't match any pattern
    # exactly — so sanitiser is a no-op and content flows through.
    big_block = ("Reasoning step. " * 50)[:600]  # ~600 chars
    final = " Final answer: 42."
    sse = _sse_lines_from_token_groups([list(big_block), [final]])
    backend = _make_backend_with_fake_stream(sse)

    out = asyncio.run(_drain(backend))
    joined = "".join(out)

    # The sanity check: the safety cap fired, so we got at least one
    # mid-stream chunk well before end-of-stream.
    assert len(out) >= 2, f"expected mid-stream flush, got {out!r}"
    # Total text preserved (sanitiser was a no-op on this block).
    assert "Final answer: 42." in joined


# ─────────────────────────── paragraph break flush


def test_paragraph_break_triggers_flush_and_passthrough() -> None:
    """A double-newline is treated as a hard 'preamble's done' signal —
    flush sanitised + switch to passthrough."""
    pre = "Let me check the data for a second.\n\n"  # matches `_COT_PREAMBLE_PATTERNS[2]`
    answer = "The capital is Paris."
    tail = " Anything else you'd like to know?"
    sse = _sse_lines_from_token_groups([list(pre + answer), [tail]])
    backend = _make_backend_with_fake_stream(sse)

    out = asyncio.run(_drain(backend))
    joined = "".join(out)
    assert "Paris" in joined
    # The "Let me check the data" preamble matches the
    # `let me (try|check|look|search|fetch|see)…[:.] ` pattern and gets peeled.
    assert "Let me check" not in joined
    # Streamed (paragraph-break flush + passthrough for tail).
    assert len(out) >= 2, f"expected ≥ 2 chunks, got {out!r}"


# ─────────────────────────── end-of-stream still works for short replies


def test_short_reply_below_threshold_still_emits() -> None:
    """A tiny reply (under EARLY_CHECK_MIN_CHARS) hits end-of-stream
    before any flush condition triggers — the existing 'flush at
    [DONE]' path must still emit it."""
    sse = _sse_lines_from_token_groups([["Hi.", " OK."]])
    backend = _make_backend_with_fake_stream(sse)

    out = asyncio.run(_drain(backend))
    joined = "".join(out)
    assert "Hi" in joined
    assert "OK" in joined


# ─────────────────────────── interrupted stream still flushes buffer


def test_interrupted_stream_flushes_buffered_prefix() -> None:
    """A07 path: stream ended without [DONE] but tokens were buffered.
    Existing behaviour must preserve — buffered prefix is sanitised +
    yielded, then the '... (response interrupted)' marker."""
    # Build SSE without [DONE] at end.
    payload = {"choices": [{"delta": {"content": "partial answer"}}]}
    sse = [
        b"data: " + json.dumps(payload).encode() + b"\n",
        b"",  # EOF without [DONE]
    ]
    backend = _make_backend_with_fake_stream(sse)

    out = asyncio.run(_drain(backend))
    joined = "".join(out)
    assert "partial answer" in joined
    assert "interrupted" in joined.lower()
