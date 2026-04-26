"""Tests for B6 (#154): Hybrid first-utterance after blip latency.

Pre-fix the first cloud-STT failure paid:
  1. The full 15 s wait_for budget (now tightened to 10 s)
  2. Moonshine cold-load on the lazy fallback (1-3 s)
  3. Moonshine transcribe (~1 s)

Post-fix:
  - swap_backends pre-warms Moonshine in the background when entering
    a cloud STT mode, so the cold-load is paid asynchronously
  - cloud-STT timeout drops from 15 s → 10 s
  - the fallback path emits a `stt_fallback_active` progress event so
    Tab5 can show feedback during the 1 s of local transcribe
"""
from __future__ import annotations

import asyncio

import pytest

from dragon_voice import pipeline as pipeline_mod
from dragon_voice.config import VoiceConfig
from dragon_voice.pipeline import VoicePipeline


# ─────────────────────────── prewarm spawn on swap into cloud STT


def test_swap_into_cloud_stt_schedules_fallback_prewarm() -> None:
    """B6 core: swap_backends → openrouter STT must spawn a background
    Moonshine prewarm task.  Pre-fix the first cloud-STT failure paid
    the full cold-load on the synchronous path."""
    cfg = VoiceConfig()

    async def on_event(e: dict) -> None:
        return None

    async def on_audio(b: bytes) -> None:
        return None

    p = VoicePipeline(cfg, on_audio=on_audio, on_event=on_event)
    # Sanity: no prewarm task before the swap.
    assert getattr(p, "_fallback_prewarm_task", None) is None

    async def go():
        # Mock the create_stt + initialize so we don't actually load
        # Moonshine in unit tests.
        prewarm_invoked = asyncio.Event()

        class _FakeStt:
            name = "fake-moonshine"

            async def initialize(self) -> None:
                prewarm_invoked.set()
                # Don't actually block — the test asserts the task was
                # SPAWNED, not that prewarm completed.

            async def shutdown(self) -> None:
                return None

            async def transcribe(self, *args, **kw) -> str:
                return ""

        from dragon_voice import stt as stt_mod
        original_create_stt = stt_mod.create_stt
        stt_mod.create_stt = lambda cfg_: _FakeStt()  # type: ignore[assignment]
        # pipeline.py imports create_stt locally inside the prewarm
        # closure so we also need to patch the module attribute that
        # `from dragon_voice.stt import create_stt` resolves to.
        try:
            p._schedule_fallback_stt_prewarm()
            task = getattr(p, "_fallback_prewarm_task", None)
            assert task is not None and not task.done(), "prewarm task should be spawned"
            await asyncio.wait_for(prewarm_invoked.wait(), timeout=1.0)
            # Task either done now or about to be — let it settle.
            try:
                await task
            except Exception:
                pass
            # _fallback_stt was populated by the task.
            assert getattr(p, "_fallback_stt", None) is not None
        finally:
            stt_mod.create_stt = original_create_stt  # type: ignore[assignment]

    asyncio.run(go())


def test_prewarm_is_idempotent_when_fallback_already_present() -> None:
    """A second swap to a cloud STT mode should NOT spawn a duplicate
    prewarm if `_fallback_stt` is already cached."""
    cfg = VoiceConfig()

    async def on_event(e: dict) -> None:
        return None

    async def on_audio(b: bytes) -> None:
        return None

    p = VoicePipeline(cfg, on_audio=on_audio, on_event=on_event)
    p._fallback_stt = object()  # already cached

    p._schedule_fallback_stt_prewarm()
    assert getattr(p, "_fallback_prewarm_task", None) is None, (
        "no task should spawn when fallback is already cached"
    )


def test_prewarm_skips_if_inflight_task_pending() -> None:
    """Concurrent cloud-STT swap calls shouldn't queue duplicate
    prewarm tasks."""
    cfg = VoiceConfig()

    async def on_event(e: dict) -> None:
        return None

    async def on_audio(b: bytes) -> None:
        return None

    p = VoicePipeline(cfg, on_audio=on_audio, on_event=on_event)

    async def go():
        # Stub out the actual prewarm with a long-running sleep.
        from dragon_voice import stt as stt_mod
        original = stt_mod.create_stt

        class _SlowStt:
            name = "slow"

            async def initialize(self) -> None:
                await asyncio.sleep(0.3)

            async def shutdown(self) -> None:
                return None

        stt_mod.create_stt = lambda cfg_: _SlowStt()  # type: ignore[assignment]
        try:
            p._schedule_fallback_stt_prewarm()
            first = p._fallback_prewarm_task
            await asyncio.sleep(0.05)
            p._schedule_fallback_stt_prewarm()
            second = p._fallback_prewarm_task
            assert first is second, "second call must reuse in-flight task"
            try:
                await first
            except Exception:
                pass
        finally:
            stt_mod.create_stt = original  # type: ignore[assignment]

    asyncio.run(go())


# ─────────────────────────── cloud STT timeout tightened


def test_cloud_stt_timeout_is_10s_not_15s() -> None:
    """B6 part 2: tightened the wait_for budget from 15 s → 10 s.
    Pin the value via source inspection so a refactor that drifts it
    back to 15 s breaks this test loudly."""
    import inspect
    src = inspect.getsource(VoicePipeline._process_utterance)
    assert "timeout=10" in src, (
        "B6 expects cloud STT wait_for(timeout=10); refactor must keep this "
        "or update the audit / this test together"
    )
    assert "timeout=15" not in src, (
        "stale timeout=15 still present — B6 wasn't applied to all sites"
    )
