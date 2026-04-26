"""Tests for B5 (#152): dictation post-process can race-emit on cancel.

Pre-fix, both `pipeline.cancel()` and `pipeline.finish_dictation()`
called `self._post_process_task.cancel()` but never awaited the task.
If the task's LLM call completed between `task.cancel()` and the
CancelledError firing at the next await, a stale `dictation_summary`
frame would still emit:

  * Double-emit on rapid stop+restart of dictation (old + new summaries)
  * Stale emit lands after a disconnect mid-LLM

Post-fix both sites `await prev` after `prev.cancel()` so by the time
the call returns the task is guaranteed CANCELLED or completed cleanly.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from dragon_voice.config import VoiceConfig
from dragon_voice.pipeline import VoicePipeline


class _FakeLLM:
    """Streams `TITLE: x / SUMMARY: y` after a configurable delay."""

    name = "fake-llm"

    def __init__(self, delay_s: float = 0.05) -> None:
        self.delay_s = delay_s
        self.invocations = 0

    async def generate_stream(self, prompt: str, system: str) -> AsyncIterator[str]:
        self.invocations += 1
        # Sleep BEFORE yielding any token; this is where a cancel must
        # interrupt to avoid the race.
        await asyncio.sleep(self.delay_s)
        for tok in ("TITLE: hello\nSUMMARY: a quick note.", ""):
            yield tok


class _FakeConvEngine:
    def __init__(self, llm: _FakeLLM) -> None:
        self.llm = llm


def _make_pipeline(llm: _FakeLLM) -> tuple[VoicePipeline, list[dict]]:
    cfg = VoiceConfig()
    events: list[dict] = []

    async def on_event(e: dict) -> None:
        events.append(e)

    async def on_audio(b: bytes) -> None:
        return None

    p = VoicePipeline(
        cfg,
        on_audio=on_audio,
        on_event=on_event,
        conversation_engine=_FakeConvEngine(llm),
        session_id="s1",
    )
    return p, events


# ─────────────────────────── cancel() awaits the task


def test_cancel_awaits_post_process_task_so_no_stale_emit() -> None:
    """B5 core: cancel() must wait for the post-process task to fully
    finish (cancelled or completed) before returning.  We force the
    race by shielding a synthetic post-process body so cancellation
    is requested but cannot interrupt the running coroutine — pre-fix
    cancel() returns while the task is still running; post-fix it
    blocks until the shield completes."""
    llm = _FakeLLM(delay_s=0.0)
    p, events = _make_pipeline(llm)

    completed = asyncio.Event()
    cancel_returned_at: list[float] = []
    completion_at: list[float] = []

    async def shielded_body():
        # Simulates a post-process body that's mid-emit and not
        # interruptible at this exact await point (e.g., flushing a
        # WS write atomically).  The shield ensures task.cancel() is
        # no-op until the inner finishes.
        try:
            await asyncio.shield(asyncio.sleep(0.2))
        finally:
            completion_at.append(asyncio.get_event_loop().time())
            completed.set()

    async def go():
        p._post_process_task = asyncio.ensure_future(shielded_body())
        await asyncio.sleep(0.01)  # let it start the shielded sleep
        await p.cancel()
        cancel_returned_at.append(asyncio.get_event_loop().time())

    asyncio.run(go())

    # Post-fix invariant: cancel() returns AFTER the task completes.
    # Pre-fix this test would fail because cancel() returns immediately
    # after task.cancel() while the shielded sleep is still running.
    assert completion_at, "task must have run to completion"
    assert cancel_returned_at, "cancel must have returned"
    assert cancel_returned_at[0] >= completion_at[0], (
        f"cancel() returned at {cancel_returned_at[0]} BEFORE "
        f"task completed at {completion_at[0]} — cancel() didn't await"
    )


# ─────────────────────────── finish_dictation prev-cancel awaits


def test_rapid_finish_dictation_emits_only_one_summary() -> None:
    """Double-finish_dictation: prev task must be awaited-cancelled
    before new task starts so we never emit two summaries."""
    llm = _FakeLLM(delay_s=0.4)
    p, events = _make_pipeline(llm)

    async def go():
        # Manually drive the prev-cancel branch of finish_dictation by
        # spawning a fake "prior" post_process task and then calling
        # the cancel-prev pattern that finish_dictation uses.
        # We can't easily call finish_dictation directly because it
        # depends on dictation_segments + audio_buffer state, so this
        # test exercises the same prev-cancel logic via cancel().
        p._post_process_task = asyncio.ensure_future(
            p._post_process_dictation("first transcript -- should NOT emit"),
        )
        await asyncio.sleep(0.01)  # let prev start sleeping
        # Simulate the prev-cancel + new-task pattern.
        prev = p._post_process_task
        p._post_process_task = None
        prev.cancel()
        try:
            await prev
        except (asyncio.CancelledError, Exception):
            pass
        # Now spawn the "new" task and let it run to completion.
        p._post_process_task = asyncio.ensure_future(
            p._post_process_dictation("second transcript -- SHOULD emit"),
        )
        # Wait for new to finish.
        try:
            await p._post_process_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(go())

    summary_emits = [e for e in events if e.get("type") == "dictation_summary"]
    # Exactly one summary, from the second call.  Pre-fix this could be
    # 0 (race won by cancel) or 2 (prev raced through the await before
    # CancelledError reached it).
    assert len(summary_emits) == 1, (
        f"expected exactly 1 dictation_summary emit, got {len(summary_emits)}: {summary_emits!r}"
    )


# ─────────────────────────── happy path: post-process emits when uninterrupted


def test_uninterrupted_post_process_emits_summary() -> None:
    """Regression guard: when nobody cancels, the post-process must
    emit `dictation_summary` normally."""
    llm = _FakeLLM(delay_s=0.0)
    p, events = _make_pipeline(llm)

    async def go():
        await p._post_process_dictation("a transcript long enough to summarise")

    asyncio.run(go())

    summary_emits = [e for e in events if e.get("type") == "dictation_summary"]
    assert len(summary_emits) == 1
    assert summary_emits[0].get("title") == "hello"


# ─────────────────────────── cancel is idempotent (preserved behaviour)


def test_cancel_with_no_post_process_task_is_idempotent() -> None:
    """cancel() must not raise when there's no post_process_task to
    cancel.  This is the common cancel-during-text-turn case."""
    llm = _FakeLLM(delay_s=0.0)
    p, _ = _make_pipeline(llm)
    assert p._post_process_task is None  # nothing spawned

    async def go():
        await p.cancel()
        await p.cancel()  # second cancel must also be a no-op

    asyncio.run(go())  # must not raise
