"""Tests for ``dragon_voice.fallback_tts_cache.FallbackTtsCache``.

Pin the lazy-load + Phase 2 L3 (#94) kill_active_procs +
shutdown invariants.  Mirror of test_fallback_stt_cache.py.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dragon_voice.fallback_tts_cache import FallbackTtsCache


def _make_tts_backend(
    *,
    audio: bytes = b"\x00\x01\x02\x03",
    has_kill: bool = True,
) -> MagicMock:
    tts = MagicMock(spec=["initialize", "synthesize", "shutdown"]
                    + (["kill_active_procs"] if has_kill else []))
    tts.initialize = AsyncMock()
    tts.synthesize = AsyncMock(return_value=audio)
    tts.shutdown = AsyncMock()
    if has_kill:
        tts.kill_active_procs = MagicMock()
    return tts


# ─── synthesize (lazy-load path) ─────────────────────────────


class TestLazyLoad:
    @pytest.mark.asyncio
    async def test_first_synthesize_cold_loads_then_synthesizes(self):
        cache = FallbackTtsCache()
        assert cache.is_loaded is False

        backend = _make_tts_backend(audio=b"piper-out")
        with patch(
            "dragon_voice.tts.create_tts", return_value=backend,
        ):
            result = await cache.synthesize("hello")

        assert result == b"piper-out"
        backend.initialize.assert_awaited_once()
        backend.synthesize.assert_awaited_once_with("hello")
        assert cache.is_loaded is True

    @pytest.mark.asyncio
    async def test_second_synthesize_reuses_cached_backend(self):
        cache = FallbackTtsCache()
        backend = _make_tts_backend()

        with patch(
            "dragon_voice.tts.create_tts", return_value=backend,
        ) as create:
            await cache.synthesize("first")
            await cache.synthesize("second")

        # create_tts invoked exactly once (cached for second call)
        assert create.call_count == 1
        backend.initialize.assert_awaited_once()
        assert backend.synthesize.await_count == 2


# ─── Phase 2 L3 kill_active_procs on failure ────────────────


class TestKillActiveProcsOnFailure:
    @pytest.mark.asyncio
    async def test_synth_failure_kills_active_procs_then_reraises(self):
        """Phase 2 L3 (#94) closure: any in-flight Piper subproc
        MUST be killed BEFORE the exception propagates so the
        audio device + FD don't leak."""
        cache = FallbackTtsCache()
        backend = _make_tts_backend()
        backend.synthesize = AsyncMock(side_effect=RuntimeError("piper crashed"))

        with patch(
            "dragon_voice.tts.create_tts", return_value=backend,
        ):
            with pytest.raises(RuntimeError, match="piper crashed"):
                await cache.synthesize("text")

        backend.kill_active_procs.assert_called_once()

    @pytest.mark.asyncio
    async def test_synth_timeout_kills_active_procs_then_reraises(self):
        """asyncio.TimeoutError path: same kill_active_procs
        invariant as the generic-exception path."""
        cache = FallbackTtsCache()
        backend = _make_tts_backend()

        async def _hang(text):
            await asyncio.sleep(10)

        backend.synthesize = AsyncMock(side_effect=_hang)

        with patch(
            "dragon_voice.tts.create_tts", return_value=backend,
        ):
            with pytest.raises(asyncio.TimeoutError):
                await cache.synthesize("text", timeout_s=0.01)

        backend.kill_active_procs.assert_called_once()

    @pytest.mark.asyncio
    async def test_backend_without_kill_active_procs_does_not_crash(self):
        """Non-Piper TTS backends don't expose kill_active_procs.
        The hasattr-gate must keep the failure path working."""
        cache = FallbackTtsCache()
        backend = _make_tts_backend(has_kill=False)
        backend.synthesize = AsyncMock(side_effect=RuntimeError("dead"))

        with patch(
            "dragon_voice.tts.create_tts", return_value=backend,
        ):
            with pytest.raises(RuntimeError):
                await cache.synthesize("text")

        # Must NOT raise AttributeError on missing kill_active_procs


# ─── Default timeout pin ─────────────────────────────────────


class TestDefaultTimeout:
    def test_default_is_90_seconds(self):
        """Audit C3 (#137) closure: Piper takes 15-25 s for a
        200-word reply on Q6A ARM64.  Default of 90 s gives
        headroom; pre-#137 used 30 s which was too tight."""
        from dragon_voice.fallback_tts_cache import (
            _DEFAULT_FALLBACK_TIMEOUT_S,
        )
        assert _DEFAULT_FALLBACK_TIMEOUT_S == 90.0

    @pytest.mark.asyncio
    async def test_custom_timeout_overrides_default(self):
        cache = FallbackTtsCache()
        backend = _make_tts_backend()

        async def _slow(text):
            await asyncio.sleep(0.1)
            return b"x"

        backend.synthesize = AsyncMock(side_effect=_slow)

        with patch(
            "dragon_voice.tts.create_tts", return_value=backend,
        ):
            # Custom 0.01 s timeout → must trip
            with pytest.raises(asyncio.TimeoutError):
                await cache.synthesize("text", timeout_s=0.01)


# ─── Shutdown ────────────────────────────────────────────────


class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_releases_cached_backend(self):
        cache = FallbackTtsCache()
        backend = _make_tts_backend()

        with patch(
            "dragon_voice.tts.create_tts", return_value=backend,
        ):
            await cache.synthesize("text")

        await cache.shutdown()

        backend.shutdown.assert_awaited_once()
        assert cache._fallback_tts is None

    @pytest.mark.asyncio
    async def test_shutdown_when_nothing_loaded_is_noop(self):
        cache = FallbackTtsCache()
        # Must NOT raise.
        await cache.shutdown()
        assert cache.is_loaded is False

    @pytest.mark.asyncio
    async def test_shutdown_swallows_backend_exception(self):
        """Pipeline shutdown shouldn't cascade — if the cached
        backend's shutdown raises, log at DEBUG but continue."""
        cache = FallbackTtsCache()
        backend = _make_tts_backend()
        backend.shutdown = AsyncMock(side_effect=RuntimeError("dead"))

        with patch(
            "dragon_voice.tts.create_tts", return_value=backend,
        ):
            await cache.synthesize("text")

        # Must NOT raise.
        await cache.shutdown()
        assert cache._fallback_tts is None
