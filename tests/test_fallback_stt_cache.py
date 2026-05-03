"""Tests for ``dragon_voice.fallback_stt_cache.FallbackSttCache``.

Pin the prewarm idempotency, lazy-load fallback, race-guard
last-writer-wins, and shutdown invariants.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dragon_voice.fallback_stt_cache import FallbackSttCache


def _make_stt_backend(*, transcript: str = "hello") -> MagicMock:
    stt = MagicMock()
    stt.initialize = AsyncMock()
    stt.transcribe = AsyncMock(return_value=transcript)
    stt.shutdown = AsyncMock()
    return stt


# ─── transcribe (lazy-load path) ─────────────────────────────


class TestTranscribeLazyLoad:
    @pytest.mark.asyncio
    async def test_first_transcribe_cold_loads_then_transcribes(self):
        cache = FallbackSttCache()
        assert cache.is_loaded is False

        backend = _make_stt_backend(transcript="result")
        with patch(
            "dragon_voice.stt.create_stt", return_value=backend,
        ):
            result = await cache.transcribe(b"audio", sample_rate=16000)

        assert result == "result"
        backend.initialize.assert_awaited_once()
        backend.transcribe.assert_awaited_once_with(b"audio", 16000)
        assert cache.is_loaded is True

    @pytest.mark.asyncio
    async def test_second_transcribe_reuses_cached_backend(self):
        cache = FallbackSttCache()
        backend = _make_stt_backend()

        with patch(
            "dragon_voice.stt.create_stt", return_value=backend,
        ) as create:
            await cache.transcribe(b"a", sample_rate=16000)
            await cache.transcribe(b"b", sample_rate=16000)

        # create_stt invoked exactly once (cached for the second call)
        assert create.call_count == 1
        backend.initialize.assert_awaited_once()
        assert backend.transcribe.await_count == 2


# ─── schedule_prewarm (idempotent + race-guard) ──────────────


class TestSchedulePrewarm:
    @pytest.mark.asyncio
    async def test_prewarm_loads_backend_in_background(self):
        cache = FallbackSttCache()
        backend = _make_stt_backend()

        with patch(
            "dragon_voice.stt.create_stt", return_value=backend,
        ):
            cache.schedule_prewarm()
            # Wait for the background task to complete
            assert cache._prewarm_task is not None
            await cache._prewarm_task

        assert cache.is_loaded is True
        backend.initialize.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_prewarm_idempotent_when_already_loaded(self):
        """schedule_prewarm called when fallback already exists
        → no-op, no second backend created."""
        cache = FallbackSttCache()
        backend = _make_stt_backend()

        # Pre-populate the cache via transcribe
        with patch(
            "dragon_voice.stt.create_stt", return_value=backend,
        ):
            await cache.transcribe(b"audio", sample_rate=16000)

        # Now try to prewarm — must no-op
        with patch(
            "dragon_voice.stt.create_stt"
        ) as create2:
            cache.schedule_prewarm()
            # No new task scheduled
            assert (
                cache._prewarm_task is None
                or cache._prewarm_task.done()
            )
            create2.assert_not_called()

    @pytest.mark.asyncio
    async def test_prewarm_idempotent_when_already_in_flight(self):
        """schedule_prewarm called twice rapidly → second call
        no-ops while the first task is still running."""
        cache = FallbackSttCache()
        backend = _make_stt_backend()

        # Make initialize hang so the prewarm task stays in-flight
        slow = asyncio.Event()

        async def _hang():
            await slow.wait()

        backend.initialize = AsyncMock(side_effect=_hang)

        with patch(
            "dragon_voice.stt.create_stt", return_value=backend,
        ) as create:
            cache.schedule_prewarm()
            await asyncio.sleep(0)  # let task start
            cache.schedule_prewarm()  # second call, should no-op
            slow.set()
            await cache._prewarm_task

        # create_stt invoked exactly once
        assert create.call_count == 1

    @pytest.mark.asyncio
    async def test_race_guard_last_writer_wins(self):
        """When prewarm finishes BUT a real cloud-STT failure
        already cold-loaded a fallback in parallel, the prewarm
        instance is shut down (not stored) — last-writer wins."""
        cache = FallbackSttCache()

        prewarm_started = asyncio.Event()
        prewarm_can_finish = asyncio.Event()
        prewarm_backend = _make_stt_backend()
        lazy_backend = _make_stt_backend()

        async def _slow_init():
            prewarm_started.set()
            await prewarm_can_finish.wait()

        prewarm_backend.initialize = AsyncMock(side_effect=_slow_init)

        # First call returns the prewarm backend; second call (the
        # lazy-load) returns a different backend.
        backends_iter = iter([prewarm_backend, lazy_backend])

        with patch(
            "dragon_voice.stt.create_stt",
            side_effect=lambda cfg: next(backends_iter),
        ):
            cache.schedule_prewarm()
            await prewarm_started.wait()
            # Prewarm is in-flight; force the lazy-load path by
            # calling transcribe directly.  This sets
            # _fallback_stt to lazy_backend.
            await cache.transcribe(b"audio", sample_rate=16000)
            # Now release the prewarm task — it should detect
            # the cache is already populated and shut DOWN its
            # own backend (last-writer-wins).
            prewarm_can_finish.set()
            await cache._prewarm_task

        # Cache holds the lazy-load backend (first writer)
        assert cache._fallback_stt is lazy_backend
        # Prewarm backend was shut down (race-guard discard)
        prewarm_backend.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_prewarm_failure_is_swallowed(self):
        """A prewarm exception logs at WARNING but doesn't
        propagate (it's a background task; nobody's awaiting it)."""
        cache = FallbackSttCache()

        with patch(
            "dragon_voice.stt.create_stt",
            side_effect=ImportError("moonshine-voice missing"),
        ):
            cache.schedule_prewarm()
            # Awaiting the task should NOT raise
            await cache._prewarm_task

        assert cache.is_loaded is False


# ─── shutdown ────────────────────────────────────────────────


class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_cancels_in_flight_prewarm(self):
        """Audit B6 closure: cancel any pending prewarm so
        shutdown doesn't have to wait for a 1-3 s model load
        just to immediately throw it away."""
        cache = FallbackSttCache()

        # Make initialize hang so the prewarm task is in-flight
        slow = asyncio.Event()

        async def _hang():
            await slow.wait()

        backend = _make_stt_backend()
        backend.initialize = AsyncMock(side_effect=_hang)

        with patch(
            "dragon_voice.stt.create_stt", return_value=backend,
        ):
            cache.schedule_prewarm()
            await asyncio.sleep(0)
            await cache.shutdown()

        # Prewarm task was cancelled (not awaited to completion)
        assert cache._prewarm_task is None
        assert cache._fallback_stt is None

    @pytest.mark.asyncio
    async def test_shutdown_releases_cached_backend(self):
        cache = FallbackSttCache()
        backend = _make_stt_backend()

        with patch(
            "dragon_voice.stt.create_stt", return_value=backend,
        ):
            await cache.transcribe(b"audio", sample_rate=16000)

        await cache.shutdown()

        backend.shutdown.assert_awaited_once()
        assert cache._fallback_stt is None

    @pytest.mark.asyncio
    async def test_shutdown_when_nothing_loaded_is_noop(self):
        """Pin: shutdown on a fresh cache must not raise."""
        cache = FallbackSttCache()
        # Must NOT raise.
        await cache.shutdown()
        assert cache.is_loaded is False

    @pytest.mark.asyncio
    async def test_shutdown_swallows_backend_exception(self):
        """If the cached backend's shutdown raises, log at DEBUG
        but don't propagate — pipeline shutdown shouldn't
        cascade."""
        cache = FallbackSttCache()
        backend = _make_stt_backend()
        backend.shutdown = AsyncMock(side_effect=RuntimeError("dead"))

        with patch(
            "dragon_voice.stt.create_stt", return_value=backend,
        ):
            await cache.transcribe(b"audio", sample_rate=16000)

        # Must NOT raise.
        await cache.shutdown()
        assert cache._fallback_stt is None
