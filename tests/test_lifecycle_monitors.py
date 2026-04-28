"""Smoke tests for ``dragon_voice/lifecycle/monitors.py``.

Coverage:
 - ``get_rss_mb`` returns a non-negative float (on Linux it should
   produce a real reading; on systems where ``/proc/self/status`` is
   unreadable it returns 0.0).
 - ``get_cpu_temp`` returns a non-negative float (same no-crash
   contract — 0.0 when no thermal zone is readable).
 - ``memory_monitor_loop`` logs + respects the warn / crit thresholds.
   We run a single iteration with patched ``asyncio.sleep`` so we
   don't wait 5 min.

Run:
    python3 -m pytest -v tests/test_lifecycle_monitors.py
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from dragon_voice.lifecycle import monitors


class HelperFunctionTests(unittest.TestCase):
    def test_get_rss_mb_returns_non_negative_float(self):
        rss = monitors.get_rss_mb()
        self.assertIsInstance(rss, float)
        self.assertGreaterEqual(rss, 0.0)

    def test_get_cpu_temp_returns_non_negative_float(self):
        # Dragon Q6A reports a reading; CI runners typically do too.
        # Either way, the contract is "non-negative float, no crash".
        temp = monitors.get_cpu_temp()
        self.assertIsInstance(temp, float)
        self.assertGreaterEqual(temp, 0.0)

    def test_get_rss_mb_survives_missing_proc(self):
        # When /proc/self/status is unavailable, returns 0.0 (not
        # raises).  Exercise the except branch.
        with patch("builtins.open", side_effect=OSError("no /proc")):
            self.assertEqual(monitors.get_rss_mb(), 0.0)


class MemoryMonitorLoopTests(unittest.TestCase):
    """Single-iteration tests for the 5-min loop.

    We patch ``asyncio.sleep`` to raise after one tick so the loop
    exits cleanly without waiting 300 s.  Thresholds are driven by
    patched ``get_rss_mb`` / ``get_cpu_temp`` + a tiny stub ``server``.
    """

    def _make_server(self, warn=3072, crit=4096):
        s = MagicMock()
        s._mem_warn_mb = warn
        s._mem_crit_mb = crit
        s._active_connections = {}
        s._config = MagicMock()
        s._conversation = MagicMock()
        s._media_pipeline = MagicMock()
        s._backend_pool = {}
        return s

    def _fake_sleep_pass_once(self):
        """Return an ``asyncio.sleep`` stub that passes once, then raises CancelledError.

        This lets the first loop iteration run its body in full, then
        exits cleanly when the loop comes around to the next sleep.
        """
        call_count = {"n": 0}

        async def fake_sleep(_):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise asyncio.CancelledError()
            # first call returns normally — loop body runs once.

        return fake_sleep, call_count

    def test_loop_runs_one_iteration_below_warn(self):
        """Below-warn path: one iteration runs, no GC, loop exits on the 2nd sleep."""
        server = self._make_server()
        fake_sleep, call_count = self._fake_sleep_pass_once()

        async def go():
            with patch.object(monitors.asyncio, "sleep", AsyncMock(side_effect=fake_sleep)), \
                 patch.object(monitors, "get_rss_mb", return_value=1024.0), \
                 patch.object(monitors, "get_cpu_temp", return_value=50.0), \
                 patch.object(monitors.gc, "collect") as mock_gc:
                with self.assertRaises(asyncio.CancelledError):
                    await monitors.memory_monitor_loop(server)
                mock_gc.assert_not_called()

        asyncio.run(go())
        # Two sleeps: one at the start of iteration 1 (which passed),
        # one at the start of iteration 2 (which cancelled).
        self.assertEqual(call_count["n"], 2)

    def test_loop_forces_gc_when_rss_over_warn(self):
        """Above-warn, below-crit path: GC is called, no pipeline drain."""
        server = self._make_server(warn=1000, crit=10000)
        fake_sleep, _ = self._fake_sleep_pass_once()
        # First RSS read is above warn; second (post-GC) is still high
        # but below crit, so no pipeline drain runs.
        rss_values = iter([2000.0, 1500.0])

        async def go():
            with patch.object(monitors.asyncio, "sleep", AsyncMock(side_effect=fake_sleep)), \
                 patch.object(monitors, "get_rss_mb", side_effect=lambda: next(rss_values, 1500.0)), \
                 patch.object(monitors, "get_cpu_temp", return_value=50.0), \
                 patch.object(monitors.gc, "collect", return_value=42) as mock_gc:
                with self.assertRaises(asyncio.CancelledError):
                    await monitors.memory_monitor_loop(server)
                mock_gc.assert_called_once()

        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
