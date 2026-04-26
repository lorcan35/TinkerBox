"""Unit tests for δ1 (H7): media cleanup runs once on startup.

Issue #114, refs #89, refs #94.

Pre-fix ``media_cleanup_loop`` slept 3600 s BEFORE the first cleanup
pass — media uploads in the first hour after boot could pile up,
especially after a crash that left orphans or after a deploy that
didn't carry previous media files.  The MediaStore has a 500 MB cap
but it's only enforced lazily by the cleanup pass.

The fix runs cleanup ONCE on entry, then enters the hourly loop.

These tests pin:
  * cleanup IS called before the first sleep
  * cleanup is also called inside the loop (existing behaviour
    preserved — the fix is *additive*, not a replacement)
  * a startup-cleanup failure does NOT prevent the periodic loop
    from running (defensive — the loop's existing per-iteration
    swallow gets the same treatment for the new entry pass)

Tests use a stub MediaStore + monkey-patched ``asyncio.sleep`` so
they run in milliseconds without actually waiting an hour.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.lifecycle import purge


def _server_with_store(cleanup_mock: AsyncMock) -> Any:
    s = MagicMock()
    s._media_store = MagicMock()
    s._media_store.cleanup = cleanup_mock
    return s


def _patch_sleep_to_cancel_after_n(monkeypatch, max_sleeps: int = 1):
    """Replace ``asyncio.sleep`` inside the purge module with a
    counter that raises CancelledError after ``max_sleeps`` calls.
    Lets the test drive the loop deterministically without
    burning real seconds."""
    n = {"count": 0}
    real_sleep = asyncio.sleep

    async def fake_sleep(secs: float, *args, **kwargs):
        n["count"] += 1
        if n["count"] > max_sleeps:
            raise asyncio.CancelledError()
        # Yield once so other tasks can run, but don't actually sleep.
        await real_sleep(0)

    monkeypatch.setattr(purge.asyncio, "sleep", fake_sleep)
    return n


def test_media_cleanup_runs_immediately_on_loop_entry(monkeypatch) -> None:
    """The headline δ1 outcome: cleanup() must be called BEFORE the
    first 3600 s sleep.  Pre-fix it ran only AFTER the sleep, so a
    fresh deploy / crash-recovery boot with backlogged orphans had
    to wait a full hour for the first sweep.

    Test discipline: cancel the FIRST sleep call.  Pre-fix code does
    sleep→cleanup, so cleanup is never reached; post-fix does
    cleanup→sleep, so cleanup is called exactly once."""
    cleanup = AsyncMock()
    server = _server_with_store(cleanup)
    _patch_sleep_to_cancel_after_n(monkeypatch, max_sleeps=0)

    async def go():
        try:
            await purge.media_cleanup_loop(server)
        except asyncio.CancelledError:
            pass

    asyncio.run(go())
    # cleanup() was called at least once BEFORE any sleep had a
    # chance to complete — that's the contract H7 needs.
    assert cleanup.await_count >= 1, (
        "Expected media_store.cleanup() to be called on loop entry "
        "BEFORE the first asyncio.sleep(3600) — H7 fix not in place"
    )


def test_media_cleanup_runs_in_loop_after_first_pass(monkeypatch) -> None:
    """Regression guard: the additive startup-pass must NOT replace
    the periodic in-loop cleanup.  Both must fire."""
    cleanup = AsyncMock()
    server = _server_with_store(cleanup)
    # Allow 2 sleeps before cancel — drives one startup pass + one
    # in-loop pass.
    _patch_sleep_to_cancel_after_n(monkeypatch, max_sleeps=2)

    async def go():
        try:
            await purge.media_cleanup_loop(server)
        except asyncio.CancelledError:
            pass

    asyncio.run(go())
    # Two calls: 1 immediate + 1 after the (mocked) sleep.
    assert cleanup.await_count >= 2, (
        f"Expected ≥2 cleanup calls (startup + 1 in-loop); got {cleanup.await_count}"
    )


def test_startup_cleanup_failure_does_not_prevent_periodic_loop(monkeypatch) -> None:
    """Defensive: if the startup pass throws (e.g. SD-card mount
    race), the loop must still enter the periodic phase.  Mirrors
    the per-iteration swallow already in the loop body."""
    # First call raises, subsequent calls succeed.
    call_results: list[Any] = [Exception("simulated startup failure"), None]

    async def cleanup_side_effect():
        result = call_results.pop(0) if call_results else None
        if isinstance(result, Exception):
            raise result
        return result

    cleanup = AsyncMock(side_effect=cleanup_side_effect)
    server = _server_with_store(cleanup)
    _patch_sleep_to_cancel_after_n(monkeypatch, max_sleeps=2)

    async def go():
        try:
            await purge.media_cleanup_loop(server)
        except asyncio.CancelledError:
            pass

    # Must NOT raise out — the loop's exception handling absorbs both
    # the failed startup pass AND the successful in-loop pass.
    asyncio.run(go())
    # Both attempts ran (the failed one + the recovery one)
    assert cleanup.await_count >= 2
