"""Test for C7 (#137): TC backend includes max_tokens in the SSE
payload so the upstream model doesn't generate beyond what we'll
accept (and bill).

Pre-fix the payload had {model, messages, stream}.  The upstream
model could generate unbounded — Dragon's server-side _SSE_MAX_TOKENS
abort fired only after we'd paid for the over-generated tokens.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from dragon_voice.config import LLMConfig
from dragon_voice.llm.tinkerclaw_llm import TinkerClawBackend, _TC_REPLY_MAX_TOKENS


def test_payload_includes_max_tokens_pre_stream() -> None:
    """The POST payload must carry `max_tokens` so the upstream caps
    early, not after we've burned tokens that would be aborted."""
    cfg = LLMConfig(backend="tinkerclaw", tinkerclaw_token="test-token")
    b = TinkerClawBackend(cfg)
    b._health_cache_ok = True
    b._health_cache_until = time.monotonic() + 30

    captured: dict = {}

    class _FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        @property
        def content(self):
            class _Reader:
                async def readline(self_inner):
                    return b"data: [DONE]\n"
            return _Reader()

    def _fake_post(url: str, json: dict, headers: dict | None = None) -> _FakeResponse:
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers or {}
        return _FakeResponse()

    session = MagicMock()
    session.closed = False
    session.post = _fake_post  # type: ignore[assignment]
    b._session = session

    async def go():
        out = []
        async for chunk in b.generate_stream_with_messages(
            [{"role": "user", "content": "hi"}]
        ):
            out.append(chunk)
        return out

    asyncio.run(go())

    assert "json" in captured, "post payload was never built"
    assert captured["json"].get("max_tokens") == _TC_REPLY_MAX_TOKENS, (
        f"expected max_tokens={_TC_REPLY_MAX_TOKENS} in payload, "
        f"got {captured['json'].get('max_tokens')!r}"
    )
    # Sanity: the rest of the schema didn't drift.
    assert captured["json"].get("stream") is True
    assert "messages" in captured["json"]
    assert "model" in captured["json"]


def test_max_tokens_constant_is_below_server_side_abort() -> None:
    """Sanity: the pre-stream cap must be well below the server-side
    abort threshold (else the cap is meaningless — we'd hit the
    server abort first)."""
    from dragon_voice.llm.tinkerclaw_llm import _SSE_MAX_TOKENS
    assert _TC_REPLY_MAX_TOKENS < _SSE_MAX_TOKENS, (
        f"_TC_REPLY_MAX_TOKENS ({_TC_REPLY_MAX_TOKENS}) must be < "
        f"_SSE_MAX_TOKENS ({_SSE_MAX_TOKENS}) so the pre-stream cap "
        f"actually capped before the server abort"
    )
