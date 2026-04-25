"""Unit tests for the WS dispatcher's per-handler task discipline.

Phase 1 of the UX-gap remediation (see docs/UX-GAPS.md / issue #91).

Before this PR, `_handle_text` / `_handle_user_media` / `config_update`
were awaited inline in the WS read loop, so a slow handler blocked the
loop from receiving the next frame — a `cancel` from Tab5 would queue
in the TCP buffer until the handler returned.  These tests exercise the
new task-spawn path via `_spawn_handler_task`.

Strategy: don't try to spin a full live aiohttp server — that's the
end-to-end sandbox bench's job (`/tmp/ws_keepalive_test.py` against
:3513 with a real Ollama model).  Instead, exercise `_spawn_handler_task`
directly with a fake handler that we can stall + cancel, and assert on
the `conn_state["handler_tasks"]` lifecycle.

Coverage:
  - Spawning a handler stores the task in `conn_state["handler_tasks"][slot]`
  - A second invocation in the same slot WAITS for the previous to finish
    (default coalesce=False, matches Tab5's "+1 QUEUED" stash semantics)
  - Coalesce mode CANCELS the previous and runs the new (matches
    config_update last-write-wins for mode toggles)
  - Cancelling the task propagates `CancelledError` into the handler so
    cleanup paths run
  - Handler-raised exceptions get logged but don't propagate to the
    spawning context (don't kill the WS loop)
  - `conn_lock` is acquired inside the spawned task (preserves US-P10)
"""
from __future__ import annotations

import asyncio

import pytest

from dragon_voice.config import VoiceConfig
from dragon_voice.server import VoiceServer


@pytest.fixture
def server() -> VoiceServer:
    """Minimal server instance — just enough to exercise the helper.
    We never call .create_app() so no aiohttp / DB setup runs."""
    return VoiceServer(VoiceConfig())


# ───────────────────────────── slot lifecycle


def test_spawn_stores_task_in_named_slot() -> None:
    srv = VoiceServer(VoiceConfig())
    conn_state: dict = {}
    started = asyncio.Event()
    finished = asyncio.Event()

    async def handler() -> None:
        started.set()
        await asyncio.sleep(0.05)
        finished.set()

    async def go() -> None:
        await srv._spawn_handler_task(conn_state, "text", handler)
        # Task is created and live before the first await yields back to us
        await asyncio.wait_for(started.wait(), timeout=0.5)
        task = conn_state["handler_tasks"]["text"]
        assert task is not None
        assert not task.done()
        await asyncio.wait_for(finished.wait(), timeout=0.5)
        await task  # let it complete cleanly
        assert task.done()

    asyncio.run(go())


def test_default_invocation_waits_for_previous_in_same_slot() -> None:
    """Without coalesce=True, a second spawn in the same slot must
    wait for the first to finish before starting (preserves order
    for text input — Tab5 already queues a 2nd text via the
    "+1 QUEUED" stash, but the server is defensive)."""
    srv = VoiceServer(VoiceConfig())
    conn_state: dict = {}

    order: list[str] = []
    first_done = asyncio.Event()

    async def first() -> None:
        order.append("first-start")
        await asyncio.sleep(0.05)
        order.append("first-done")
        first_done.set()

    async def second() -> None:
        order.append("second-start")
        await asyncio.sleep(0.01)
        order.append("second-done")

    async def go() -> None:
        await srv._spawn_handler_task(conn_state, "text", first)
        # Spawn second immediately; should NOT preempt the first.
        await srv._spawn_handler_task(conn_state, "text", second)
        # By this point both tasks have either run or are about to.
        await asyncio.wait_for(conn_state["handler_tasks"]["text"], timeout=1)

    asyncio.run(go())

    # Strict ordering: first runs to completion, then second runs.
    assert order == ["first-start", "first-done", "second-start", "second-done"]


def test_coalesce_cancels_previous_and_runs_new() -> None:
    """With coalesce=True, a second spawn cancels the in-flight first
    and runs the new one immediately (matches config_update
    last-write-wins for rapid mode toggles)."""
    srv = VoiceServer(VoiceConfig())
    conn_state: dict = {}

    first_started = asyncio.Event()
    first_was_cancelled = asyncio.Event()
    second_done = asyncio.Event()

    async def first() -> None:
        first_started.set()
        try:
            await asyncio.sleep(10)  # would be slow if not cancelled
        except asyncio.CancelledError:
            first_was_cancelled.set()
            raise

    async def second() -> None:
        await asyncio.sleep(0.01)
        second_done.set()

    async def go() -> None:
        await srv._spawn_handler_task(conn_state, "config", first)
        await asyncio.wait_for(first_started.wait(), timeout=0.5)
        # Coalesce: cancel the in-flight first, run the second.
        await srv._spawn_handler_task(conn_state, "config", second, coalesce=True)
        await asyncio.wait_for(second_done.wait(), timeout=0.5)

    asyncio.run(go())

    assert first_was_cancelled.is_set(), "first task should have been cancelled"
    assert second_done.is_set(), "second task should have completed"


# ───────────────────────────── cancellation propagation


def test_cancelling_slot_propagates_cancellederror_into_handler() -> None:
    """When the slot's task is cancelled (e.g. by the cancel cmd),
    the handler must receive CancelledError so cleanup paths can run.
    """
    srv = VoiceServer(VoiceConfig())
    conn_state: dict = {}

    cancel_seen = asyncio.Event()
    cleanup_ran = asyncio.Event()

    async def handler() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancel_seen.set()
            # Simulate cleanup work the handler might do
            cleanup_ran.set()
            raise

    async def go() -> None:
        await srv._spawn_handler_task(conn_state, "text", handler)
        await asyncio.sleep(0.01)  # let task start
        task = conn_state["handler_tasks"]["text"]
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(go())

    assert cancel_seen.is_set()
    assert cleanup_ran.is_set()


def test_handler_exception_does_not_kill_spawning_context() -> None:
    """If the handler raises, the wrapper must catch + log so the
    WS read loop (the spawning context) doesn't die.  This mirrors
    the existing inline behavior — handlers already had try/except
    around their bodies — but explicitly verified for the new
    task-wrapped path."""
    srv = VoiceServer(VoiceConfig())
    conn_state: dict = {}

    async def boom() -> None:
        raise ValueError("intentional test failure")

    async def go() -> None:
        # The wrapper logs the exception but does NOT re-raise to the caller.
        await srv._spawn_handler_task(conn_state, "text", boom)
        # Wait for the spawned task to reach its done state.
        task = conn_state["handler_tasks"]["text"]
        try:
            await task
        except ValueError:
            # Task itself surfaces the exception when awaited; that's normal.
            pass
        assert task.done()

    asyncio.run(go())


# ───────────────────────────── conn_lock semantics


def test_conn_lock_acquired_inside_spawned_task() -> None:
    """The lock acquisition is now inside `_run`, not in the WS loop.
    Two tasks holding the same lock should serialize correctly even
    though they were spawned without awaiting (default queueing means
    the second waits for the first slot, but with different slots they
    should still serialize on the lock)."""
    srv = VoiceServer(VoiceConfig())
    conn_state: dict = {}
    conn_lock = asyncio.Lock()

    order: list[str] = []

    async def held(name: str) -> None:
        order.append(f"{name}-enter")
        await asyncio.sleep(0.05)
        order.append(f"{name}-exit")

    async def go() -> None:
        # Spawn two tasks in DIFFERENT slots so the slot-queue check
        # doesn't gate them; the lock must serialize them instead.
        await srv._spawn_handler_task(conn_state, "text", held, "text", conn_lock=conn_lock)
        await srv._spawn_handler_task(conn_state, "media", held, "media", conn_lock=conn_lock)
        await asyncio.gather(
            conn_state["handler_tasks"]["text"],
            conn_state["handler_tasks"]["media"],
        )

    asyncio.run(go())

    # Strict serialization: text fully completes before media starts.
    assert order == ["text-enter", "text-exit", "media-enter", "media-exit"]


def test_disconnect_cleanup_pattern_works() -> None:
    """Sketch of what _handle_disconnect now does: cancel any live
    handler tasks before pipeline.shutdown.  We don't invoke the full
    _handle_disconnect (that needs DB + session manager etc.) but we
    verify the cancel-and-await pattern works for the handler_tasks
    dict shape that _handle_disconnect operates on."""
    srv = VoiceServer(VoiceConfig())
    conn_state: dict = {}

    async def long_handler() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise

    async def go() -> None:
        await srv._spawn_handler_task(conn_state, "text", long_handler)
        await srv._spawn_handler_task(conn_state, "media", long_handler)
        await asyncio.sleep(0.01)  # let both start

        # Mirror _handle_disconnect's cleanup loop
        live = [t for t in conn_state["handler_tasks"].values() if t and not t.done()]
        assert len(live) == 2
        for t in live:
            t.cancel()
        await asyncio.gather(*live, return_exceptions=True)

        for t in live:
            assert t.done()

    asyncio.run(go())
