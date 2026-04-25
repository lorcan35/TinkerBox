"""Unit tests for the TinkerClaw fast-fail health check.

γ2-M6 (issue #106) of the UX-gap remediation (refs #89, #101, #94).

Pre-fix the TC backend ate the full 600 s ``sock_read`` timeout when
the gateway was reachable at TCP layer but hung / dead at app layer
— users saw nothing for 10 minutes before the connection-error
fallback fired.  The fix adds a cached pre-request ``/health`` probe
(5 s timeout, 30 s TTL) and raises a structured ``DragonError`` with
``code="gateway_unreachable"``, ``severity=FATAL``, ``scope=GATEWAY``
when it fails.

These tests pin:
  * the cache TTL behaviour (no spam-probing on every turn)
  * ``initialize()`` seeds the cache so the first request is fast
  * ``generate_stream_with_messages`` raises ``DragonError`` with the
    γ1 taxonomy when health is bad
  * the ``force=True`` escape hatch works (for ops debugging)

aiohttp is mocked at the ``ClientSession`` level — the real network
is never touched, so these run in the CI named-set without a live
gateway.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from dragon_voice.config import LLMConfig
from dragon_voice.errors import DragonError, Scope, Severity
from dragon_voice.llm.tinkerclaw_llm import (
    HEALTH_CACHE_TTL_S,
    HEALTH_CHECK_TIMEOUT_S,
    TinkerClawBackend,
)


def _backend(token: str = "test-token") -> TinkerClawBackend:
    cfg = LLMConfig(
        backend="tinkerclaw",
        tinkerclaw_url="http://localhost:18789",
        tinkerclaw_token=token,
    )
    return TinkerClawBackend(cfg)


class _FakeHealthResponse:
    """Minimal aiohttp response stand-in for /health probes."""

    def __init__(self, status: int):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeSession:
    """In-memory aiohttp ClientSession that records GET /health calls
    and returns scripted responses (or raises a scripted error)."""

    def __init__(self, responses=None, raises=None):
        self._responses = responses or []
        self._raises = raises  # exception to raise on every call, or None
        self.get_calls: list[tuple[str, float | None]] = []
        self.closed = False

    def get(self, url: str, *, timeout=None, **kwargs):
        # Record the timeout so tests can assert HEALTH_CHECK_TIMEOUT_S.
        timeout_s = (
            timeout.total if isinstance(timeout, aiohttp.ClientTimeout) else None
        )
        self.get_calls.append((url, timeout_s))
        if self._raises is not None:
            raise self._raises
        if not self._responses:
            raise RuntimeError("FakeSession ran out of scripted responses")
        return self._responses.pop(0)

    async def close(self):
        self.closed = True


# ───────────────────────── caching


def test_constants_are_sensible() -> None:
    """Pin the wire-side budget so a future tweak doesn't silently
    blow past Tab5's PONG-watch (~30 s) or hammer the gateway."""
    # 5 s probe — Tab5's PONG-watch tolerates this without churn.
    assert 1 <= HEALTH_CHECK_TIMEOUT_S <= 10
    # 30 s cache — avoids re-probing on every turn while keeping
    # recovery latency low after the gateway comes back.
    assert 10 <= HEALTH_CACHE_TTL_S <= 120


def test_is_healthy_caches_within_ttl() -> None:
    """Two calls in quick succession should result in exactly ONE
    GET /health round-trip — the second hits the cache."""
    backend = _backend()
    session = _FakeSession(responses=[_FakeHealthResponse(200)])
    backend._session = session

    async def go():
        ok1 = await backend.is_healthy()
        ok2 = await backend.is_healthy()
        return ok1, ok2

    ok1, ok2 = asyncio.run(go())
    assert ok1 is True
    assert ok2 is True
    assert len(session.get_calls) == 1, (
        f"Expected cache hit on second call; got {session.get_calls}"
    )


def test_is_healthy_force_bypasses_cache() -> None:
    """``force=True`` is the ops-debug escape hatch — must always
    re-probe regardless of cache freshness."""
    backend = _backend()
    session = _FakeSession(
        responses=[_FakeHealthResponse(200), _FakeHealthResponse(200)]
    )
    backend._session = session

    async def go():
        await backend.is_healthy()
        await backend.is_healthy(force=True)

    asyncio.run(go())
    assert len(session.get_calls) == 2


def test_is_healthy_uses_short_timeout() -> None:
    """The probe must use ``HEALTH_CHECK_TIMEOUT_S`` (5 s) — NOT the
    600 s default ``ClientSession`` timeout that's tuned for full
    agent runs.  This is the entire point of M6."""
    backend = _backend()
    session = _FakeSession(responses=[_FakeHealthResponse(200)])
    backend._session = session

    asyncio.run(backend.is_healthy())
    _url, timeout_s = session.get_calls[0]
    assert timeout_s == HEALTH_CHECK_TIMEOUT_S


def test_is_healthy_returns_false_on_non_200() -> None:
    backend = _backend()
    session = _FakeSession(responses=[_FakeHealthResponse(503)])
    backend._session = session

    assert asyncio.run(backend.is_healthy()) is False


def test_is_healthy_returns_false_on_client_error() -> None:
    """Connection refused / DNS failure / etc. — must catch and
    return False, not bubble the exception up to the caller."""
    backend = _backend()
    session = _FakeSession(raises=aiohttp.ClientError("connection refused"))
    backend._session = session

    assert asyncio.run(backend.is_healthy()) is False


def test_is_healthy_returns_false_on_asyncio_timeout() -> None:
    """Hung gateway — the probe itself times out at HEALTH_CHECK_TIMEOUT_S.
    Must come back False, not raise."""
    backend = _backend()
    session = _FakeSession(raises=asyncio.TimeoutError())
    backend._session = session

    assert asyncio.run(backend.is_healthy()) is False


def test_is_healthy_re_probes_after_ttl_expires() -> None:
    """After cache TTL expires, the next call MUST re-probe.  Pin this
    so a future refactor that uses ``time.time()`` instead of
    ``time.monotonic()`` (or vice versa) doesn't break recovery
    detection."""
    backend = _backend()
    session = _FakeSession(
        responses=[_FakeHealthResponse(200), _FakeHealthResponse(200)]
    )
    backend._session = session

    async def go():
        await backend.is_healthy()
        # Force cache expiry by rewinding the recorded time.
        backend._health_cache_until = time.monotonic() - 1.0
        await backend.is_healthy()

    asyncio.run(go())
    assert len(session.get_calls) == 2


# ───────────────────────── initialize() seeds the cache


def test_initialize_seeds_cache_on_healthy_gateway() -> None:
    """``initialize()`` already does a one-shot health check; it must
    feed the result into the cache so the first request after boot
    is fast (no extra 5 s probe just because we forgot we already
    checked)."""
    backend = _backend()

    async def go():
        with patch("aiohttp.ClientSession") as mock_cs:
            session = _FakeSession(responses=[_FakeHealthResponse(200)])
            mock_cs.return_value = session
            await backend.initialize()
        # Now is_healthy() should hit the cache, not re-probe.
        return await backend.is_healthy()

    ok = asyncio.run(go())
    assert ok is True
    # The fake session received exactly one /health call (the seed),
    # not two (seed + post-init probe).
    assert len(backend._session.get_calls) == 1


def test_initialize_marks_unhealthy_on_failure() -> None:
    """If the gateway is down at boot, the cache must reflect that —
    the first request then raises DragonError immediately instead
    of attempting a fresh probe."""
    backend = _backend()

    async def go():
        with patch("aiohttp.ClientSession") as mock_cs:
            session = _FakeSession(raises=aiohttp.ClientError("refused"))
            mock_cs.return_value = session
            await backend.initialize()

    asyncio.run(go())
    assert backend._health_cache_ok is False
    assert backend._health_cache_until > time.monotonic()


# ───────────────────────── generate_stream_with_messages raises structured error


def test_generate_stream_raises_dragon_error_when_unhealthy() -> None:
    """The headline M6 outcome: don't waste 600 s on a known-down
    gateway — raise a structured γ1-taxonomy error immediately."""
    backend = _backend()
    # Pre-populate cache as unhealthy (avoid actual probe).
    backend._health_cache_ok = False
    backend._health_cache_until = time.monotonic() + 30.0
    backend._session = _FakeSession()  # no responses — must NOT be touched

    async def go():
        async for _token in backend.generate_stream_with_messages(
            [{"role": "user", "content": "hi"}]
        ):
            pass

    with pytest.raises(DragonError) as exc_info:
        asyncio.run(go())
    err = exc_info.value
    assert err.code == "gateway_unreachable"
    assert err.severity is Severity.FATAL
    assert err.scope is Scope.GATEWAY
    # User-facing message must NOT leak implementation details
    assert "600" not in err.message
    assert "sock_read" not in err.message
    assert "aiohttp" not in err.message


def test_generate_stream_does_not_post_when_unhealthy() -> None:
    """Belt-and-suspenders: an unhealthy gateway means we MUST short-
    circuit before issuing the POST.  This test guards against a
    future refactor that catches DragonError too early and falls
    through to the request loop."""
    backend = _backend()
    backend._health_cache_ok = False
    backend._health_cache_until = time.monotonic() + 30.0
    session = _FakeSession()
    session.post = MagicMock(
        side_effect=AssertionError("must NOT call POST when unhealthy")
    )
    backend._session = session

    async def go():
        async for _token in backend.generate_stream_with_messages(
            [{"role": "user", "content": "hi"}]
        ):
            pass

    with pytest.raises(DragonError):
        asyncio.run(go())
    # The post mock was never invoked
    session.post.assert_not_called()


def test_generate_stream_proceeds_when_healthy() -> None:
    """Regression guard: when the cache says healthy, the POST path
    must run normally.  Verifies the fast-fail gate doesn't block
    the happy path."""
    backend = _backend()
    backend._health_cache_ok = True
    backend._health_cache_until = time.monotonic() + 30.0

    # Mock the POST + SSE response.  We just need it to short-circuit
    # past the SSE loop so the stream completes without yielding.
    class _FakePostResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        @property
        def content(self):
            class _Reader:
                async def readline(self_inner):
                    return b""  # EOF immediately
            return _Reader()

    session = MagicMock()
    session.closed = False
    session.post = MagicMock(return_value=_FakePostResponse())
    backend._session = session

    async def go():
        out: list[str] = []
        async for token in backend.generate_stream_with_messages(
            [{"role": "user", "content": "hi"}]
        ):
            out.append(token)
        return out

    out = asyncio.run(go())
    # Empty stream + no [DONE] → the connection-drop fallback fires.
    # Important assertion: POST was actually called (we didn't fast-
    # fail) and we got the friendly fallback string back.
    session.post.assert_called_once()
    assert out  # something was yielded — the existing fallback path
