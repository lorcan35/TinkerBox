"""Tests for the OpenRouter TTS backend's error-raising behaviour
(audit B3 / #146).

Pre-fix, every failure mode in `OpenRouterTTSBackend.synthesize`
returned `b""` silently, which made the pipeline's existing
`except (Exception, asyncio.TimeoutError)` fallback at
`pipeline.py:1224` never fire.  Net effect: no audio AND no error
event AND no fallback to local Piper — the user heard nothing.

Post-fix, each failure mode raises a `DragonError` so the existing
fallback path triggers automatically.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import aiohttp
import pytest

from dragon_voice.config import TTSConfig
from dragon_voice.errors import DragonError, Scope, Severity
from dragon_voice.tts.openrouter_tts import OpenRouterTTSBackend


def _backend() -> OpenRouterTTSBackend:
    cfg = TTSConfig(backend="openrouter", openrouter_api_key="test-key")
    return OpenRouterTTSBackend(cfg)


class _FakeResponse:
    def __init__(self, status: int, body_lines: list[bytes]) -> None:
        self.status = status
        self._body_lines = body_lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self) -> str:
        return b"".join(self._body_lines).decode("utf-8", errors="replace")

    @property
    def content(self):
        outer_self = self

        class _AsyncIter:
            def __aiter__(self_inner):
                return self_inner

            async def __anext__(self_inner):
                if not outer_self._body_lines:
                    raise StopAsyncIteration
                return outer_self._body_lines.pop(0)

        return _AsyncIter()


def _make_backend_with_response(resp: _FakeResponse) -> OpenRouterTTSBackend:
    b = _backend()
    session = MagicMock()
    session.closed = False
    session.post = MagicMock(return_value=resp)
    b._session = session
    return b


# ─────────────────────────── HTTP non-200 → DragonError


def test_http_error_raises_dragon_error_with_tts_scope() -> None:
    resp = _FakeResponse(401, [b'{"error":{"message":"Invalid API key"}}'])
    b = _make_backend_with_response(resp)
    with pytest.raises(DragonError) as exc:
        asyncio.run(b.synthesize("hello"))
    assert exc.value.code == "tts_http_error"
    assert exc.value.severity == Severity.TRANSIENT
    assert exc.value.scope == Scope.TTS
    # User-facing message must NOT leak the raw API JSON.
    assert "Invalid API key" not in exc.value.message
    assert "switching to local" in exc.value.message.lower()


# ─────────────────────────── empty SSE → DragonError


def test_empty_sse_raises_dragon_error() -> None:
    """SSE stream that arrives but contains no audio.data deltas."""
    sse_lines = [
        b'data: {"choices":[{"delta":{}}]}\n',  # no audio key
        b'data: [DONE]\n',
    ]
    resp = _FakeResponse(200, sse_lines)
    b = _make_backend_with_response(resp)
    with pytest.raises(DragonError) as exc:
        asyncio.run(b.synthesize("hello"))
    assert exc.value.code == "tts_empty_response"
    assert exc.value.severity == Severity.TRANSIENT
    assert exc.value.scope == Scope.TTS


# ─────────────────────────── network failure → DragonError


def test_network_exception_wraps_in_dragon_error() -> None:
    """A connection-level error (DNS, socket reset, etc.) inside the
    POST should surface as a structured DragonError carrying the
    original cause."""
    b = _backend()
    session = MagicMock()
    session.closed = False

    class _BoomCtx:
        async def __aenter__(self):
            raise aiohttp.ClientConnectionError("connection reset")

        async def __aexit__(self, *args):
            return False

    session.post = MagicMock(return_value=_BoomCtx())
    b._session = session

    with pytest.raises(DragonError) as exc:
        asyncio.run(b.synthesize("hello"))
    assert exc.value.code == "tts_request_failed"
    assert exc.value.severity == Severity.TRANSIENT
    assert exc.value.scope == Scope.TTS
    # `cause` should hold the original aiohttp exception for log diagnostics.
    assert isinstance(exc.value.cause, aiohttp.ClientConnectionError)


# ─────────────────────────── happy path still returns bytes


def test_happy_path_returns_pcm_bytes() -> None:
    """Regression guard: the new error handling must not break the
    successful path.  A normal SSE stream with one audio.data delta
    should decode into raw PCM bytes."""
    import base64
    pcm_payload = bytes(range(64))  # arbitrary 64 bytes of "audio"
    audio_b64 = base64.b64encode(pcm_payload).decode()
    sse_lines = [
        ("data: " + json.dumps({
            "choices": [{"delta": {"audio": {"data": audio_b64}}}]
        }) + "\n").encode(),
        b'data: [DONE]\n',
    ]
    resp = _FakeResponse(200, sse_lines)
    b = _make_backend_with_response(resp)
    out = asyncio.run(b.synthesize("hello"))
    assert isinstance(out, bytes)
    assert len(out) == 64
    assert out == pcm_payload
