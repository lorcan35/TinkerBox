"""Tests for ``dragon_voice.handler_task_spawn``.

Pin every spawn / coalesce / queue / lock / failure branch.
"""
from __future__ import annotations

import asyncio

import pytest

from dragon_voice.handler_task_spawn import spawn_handler_task


# ─── Basic spawn ─────────────────────────────────────────────


class TestBasicSpawn:
    @pytest.mark.asyncio
    async def test_spawn_creates_tracked_task(self):
        ran = asyncio.Event()

        async def _handler():
            ran.set()
            await asyncio.sleep(0)

        conn_state: dict = {}
        await spawn_handler_task(conn_state, "text", _handler)

        # Task exists in the slot
        task = conn_state["handler_tasks"]["text"]
        assert task is not None
        # Wait for the spawned task to complete
        await task
        assert ran.is_set()

    @pytest.mark.asyncio
    async def test_handler_args_passed_through(self):
        captured: list = []

        async def _handler(a, b, c):
            captured.append((a, b, c))

        conn_state: dict = {}
        await spawn_handler_task(
            conn_state, "media", _handler, "x", "y", "z",
        )
        await conn_state["handler_tasks"]["media"]

        assert captured == [("x", "y", "z")]

    @pytest.mark.asyncio
    async def test_task_named_for_slot(self):
        async def _handler():
            await asyncio.sleep(0)

        conn_state: dict = {}
        await spawn_handler_task(conn_state, "config", _handler)
        task = conn_state["handler_tasks"]["config"]
        assert task.get_name() == "ws_handler:config"
        await task


# ─── Queue (default coalesce=False) ──────────────────────────


class TestQueueSemantic:
    @pytest.mark.asyncio
    async def test_second_call_waits_for_first(self):
        """Default coalesce=False: second call must WAIT for the
        first to finish.  Preserves text-turn ordering."""
        log: list[str] = []

        async def _slow():
            log.append("first-start")
            await asyncio.sleep(0.02)
            log.append("first-done")

        async def _fast():
            log.append("second-start")
            await asyncio.sleep(0.005)
            log.append("second-done")

        conn_state: dict = {}
        await spawn_handler_task(conn_state, "text", _slow)
        # Tiny pause so _slow gets started
        await asyncio.sleep(0)
        # This call must wait for _slow to finish before _fast starts
        await spawn_handler_task(conn_state, "text", _fast)
        await conn_state["handler_tasks"]["text"]

        assert log == [
            "first-start", "first-done", "second-start", "second-done",
        ]


# ─── Coalesce (coalesce=True) ────────────────────────────────


class TestCoalesceSemantic:
    @pytest.mark.asyncio
    async def test_coalesce_cancels_inflight(self):
        """coalesce=True: inflight task gets cancelled before
        the new one runs.  Used for config_update where last-
        write-wins."""
        cancelled_log: list[str] = []
        ran_log: list[str] = []

        async def _slow():
            try:
                await asyncio.sleep(10)  # never finishes naturally
                ran_log.append("slow-finished")
            except asyncio.CancelledError:
                cancelled_log.append("slow-cancelled")
                raise

        async def _new():
            ran_log.append("new-ran")

        conn_state: dict = {}
        await spawn_handler_task(conn_state, "config", _slow)
        await asyncio.sleep(0)
        # coalesce=True → cancels _slow
        await spawn_handler_task(
            conn_state, "config", _new, coalesce=True,
        )
        await conn_state["handler_tasks"]["config"]

        assert "slow-cancelled" in cancelled_log
        assert "slow-finished" not in ran_log
        assert "new-ran" in ran_log


# ─── Conn lock acquisition ───────────────────────────────────


class TestConnLockAcquired:
    @pytest.mark.asyncio
    async def test_handler_waits_for_conn_lock_when_provided(self):
        """When conn_lock is provided, the spawned task acquires
        it before calling the handler.  Pin the US-P10
        serialisation invariant."""
        lock = asyncio.Lock()
        await lock.acquire()
        ran = asyncio.Event()

        async def _handler():
            ran.set()

        conn_state: dict = {}
        await spawn_handler_task(
            conn_state, "text", _handler, conn_lock=lock,
        )
        # _handler hasn't run because lock is held externally
        await asyncio.sleep(0.005)
        assert not ran.is_set()
        # Release → handler proceeds
        lock.release()
        await conn_state["handler_tasks"]["text"]
        assert ran.is_set()

    @pytest.mark.asyncio
    async def test_no_lock_runs_immediately(self):
        ran = asyncio.Event()

        async def _handler():
            ran.set()

        conn_state: dict = {}
        await spawn_handler_task(conn_state, "text", _handler)
        await conn_state["handler_tasks"]["text"]
        assert ran.is_set()


# ─── Failure isolation ───────────────────────────────────────


class TestFailureIsolation:
    @pytest.mark.asyncio
    async def test_handler_exception_does_not_propagate_to_caller(self):
        """If the handler raises, the WS read loop must NOT see
        the exception (the spawned task swallows + logs).  Pin
        so the WS loop stays alive across handler bugs."""
        async def _broken():
            raise RuntimeError("handler bug")

        conn_state: dict = {}
        # Spawning must not raise
        await spawn_handler_task(conn_state, "text", _broken)
        # Awaiting the spawned task must not raise either
        # (the inner _run swallows non-CancelledError)
        await conn_state["handler_tasks"]["text"]

    @pytest.mark.asyncio
    async def test_handler_cancellederror_propagates_to_task(self):
        """CancelledError MUST re-propagate inside the task so
        the task transitions to CANCELLED state (not FINISHED).
        cancel_handler relies on cancelled() vs. done() distinction."""

        ready = asyncio.Event()

        async def _hang():
            ready.set()
            await asyncio.sleep(60)

        conn_state: dict = {}
        await spawn_handler_task(conn_state, "text", _hang)
        # Wait for the handler to actually start
        await ready.wait()

        task = conn_state["handler_tasks"]["text"]
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert task.cancelled()


# ─── handler_tasks dict initialised on first spawn ───────────


class TestStateInit:
    @pytest.mark.asyncio
    async def test_handler_tasks_dict_created_when_missing(self):
        async def _h():
            pass

        conn_state: dict = {}  # no handler_tasks yet
        await spawn_handler_task(conn_state, "text", _h)
        assert "handler_tasks" in conn_state
        await conn_state["handler_tasks"]["text"]

    @pytest.mark.asyncio
    async def test_existing_handler_tasks_preserved(self):
        """Existing slots in handler_tasks must NOT be wiped by
        the setdefault."""
        async def _h():
            pass

        existing_other_slot = object()  # sentinel value
        conn_state: dict = {
            "handler_tasks": {"media": existing_other_slot},
        }
        await spawn_handler_task(conn_state, "text", _h)
        assert conn_state["handler_tasks"]["media"] is existing_other_slot
        await conn_state["handler_tasks"]["text"]


# ─── Done previous task is fine ──────────────────────────────


class TestDonePreviousTask:
    @pytest.mark.asyncio
    async def test_done_previous_task_does_not_block(self):
        """Previous task already finished — new spawn doesn't
        wait/cancel.  Pin the `prev.done()` shortcut."""
        log: list[str] = []

        async def _quick():
            log.append("quick-1")

        async def _second():
            log.append("second")

        conn_state: dict = {}
        await spawn_handler_task(conn_state, "text", _quick)
        await conn_state["handler_tasks"]["text"]
        # First task is done; second should run promptly
        await spawn_handler_task(conn_state, "text", _second)
        await conn_state["handler_tasks"]["text"]
        assert log == ["quick-1", "second"]
