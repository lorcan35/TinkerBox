"""Tests for ``dragon_voice.cap_downgrade.maybe_speak_cap_downgrade_alert``.

Pin five branches:

  1. **Happy path** — cmd reports cap_downgrade + pipeline has
     speak_system → task is spawned + tracked in bg_tasks.
  2. **Wrong reason** — cmd has a different reason → no task.
  3. **No pipeline** — connection still booting → no task, no crash.
  4. **Pipeline lacks speak_system** — e.g. a stub in tests → no task.
  5. **Failure isolation** — anything raising inside the function is
     logged + swallowed so the config_update flow keeps going.

Tests are sync (no @pytest.mark.asyncio) because the function under
test is sync — it spawns the task via `asyncio.create_task` but doesn't
await it.  Tests run inside an asyncio.Loop via `asyncio.run` so
`create_task` has a loop to attach to.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from dragon_voice.cap_downgrade import maybe_speak_cap_downgrade_alert


def _make_conn_state(pipeline=None) -> dict:
    """Plain-dict conn_state stub (mirrors test_config_update_*.py shape)."""
    return {"pipeline": pipeline, "bg_tasks": set()}


def _make_pipeline_with_speak_system() -> MagicMock:
    p = MagicMock()
    p.speak_system = AsyncMock()
    return p


class CapDowngradeAlertTests(unittest.TestCase):
    def test_happy_path_spawns_tracked_task(self):
        async def go():
            pipeline = _make_pipeline_with_speak_system()
            conn = _make_conn_state(pipeline=pipeline)
            cmd = {"reason": "cap_downgrade", "voice_mode": 0}

            maybe_speak_cap_downgrade_alert(cmd, conn)

            # Task spawned + tracked.
            self.assertEqual(len(conn["bg_tasks"]), 1)
            # Wait for it to actually run.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            pipeline.speak_system.assert_awaited_once_with(
                "Daily budget cap reached. Switched back to local mode."
            )

        asyncio.run(go())

    def test_wrong_reason_does_nothing(self):
        async def go():
            pipeline = _make_pipeline_with_speak_system()
            conn = _make_conn_state(pipeline=pipeline)
            for reason in (None, "", "user_initiated", "swap", "tc_fallback"):
                cmd = {"reason": reason}
                maybe_speak_cap_downgrade_alert(cmd, conn)
            self.assertEqual(len(conn["bg_tasks"]), 0)
            pipeline.speak_system.assert_not_called()

        asyncio.run(go())

    def test_no_pipeline_does_not_crash(self):
        """During boot the pipeline isn't wired yet — a config_update
        with cap_downgrade reason must skip silently."""
        async def go():
            conn = _make_conn_state(pipeline=None)
            cmd = {"reason": "cap_downgrade"}
            maybe_speak_cap_downgrade_alert(cmd, conn)
            self.assertEqual(len(conn["bg_tasks"]), 0)

        asyncio.run(go())

    def test_pipeline_without_speak_system_does_nothing(self):
        """A stub pipeline (test path or pre-Wave-14 backend) doesn't
        have speak_system; we must skip cleanly."""
        async def go():
            pipeline = MagicMock(spec=[])  # empty spec — no methods
            conn = _make_conn_state(pipeline=pipeline)
            cmd = {"reason": "cap_downgrade"}
            maybe_speak_cap_downgrade_alert(cmd, conn)
            self.assertEqual(len(conn["bg_tasks"]), 0)

        asyncio.run(go())

    def test_failure_isolation_swallows_all(self):
        """If anything raises (e.g. bg_tasks missing from conn_state),
        the function logs and returns cleanly — must not crash up to
        the caller."""
        async def go():
            pipeline = _make_pipeline_with_speak_system()
            # Conn state missing bg_tasks key — will raise KeyError
            # at `conn_state["bg_tasks"]`.
            conn = {"pipeline": pipeline}
            # Must NOT raise.
            maybe_speak_cap_downgrade_alert({"reason": "cap_downgrade"}, conn)

        asyncio.run(go())

    def test_done_callback_discards_from_bg_tasks(self):
        """When the spawned task completes, it removes itself from
        bg_tasks so the set doesn't grow unbounded across many
        cap_downgrade events in a long-lived session."""
        async def go():
            pipeline = _make_pipeline_with_speak_system()
            conn = _make_conn_state(pipeline=pipeline)

            maybe_speak_cap_downgrade_alert({"reason": "cap_downgrade"}, conn)
            self.assertEqual(len(conn["bg_tasks"]), 1)

            # Drain pending callbacks.  speak_system is an AsyncMock
            # that returns immediately, so the task completes after
            # one event-loop iteration; the done-callback runs the
            # iteration after.
            for _ in range(5):
                await asyncio.sleep(0)

            self.assertEqual(len(conn["bg_tasks"]), 0)

        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
