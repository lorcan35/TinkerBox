"""W7-B: agent-skill catalog API tests.

Verifies that GET /api/v1/agent_skills:
  * returns the full static OpenClaw core-tool list when the agent_log
    ring is empty (initial Dragon boot)
  * merges observed tool names from the ring on top of the static set
    (so gateway tools surface as soon as they fire)
  * never returns the same tool twice (static + observed dedupe)
  * carries `uses` + `last_ts` derived from the ring
  * sorts observed-only extras most-recently-used first
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock

from dragon_voice.api import agent_log as _alog
from dragon_voice.api.agent_skills import (
    AgentSkillsRoutes,
    _STATIC_AGENT_SKILLS,
    build_catalog,
)


class _BaseRingTest(unittest.TestCase):
    """Snapshot + restore the process-global agent_log ring so each
    test starts clean and doesn't bleed state into the next."""

    def setUp(self):
        with _alog._lock:
            self._saved_ring = list(_alog._ring)
            self._saved_next = _alog._next_id
            _alog._ring.clear()
            _alog._next_id = 1

    def tearDown(self):
        with _alog._lock:
            _alog._ring.clear()
            for item in self._saved_ring:
                _alog._ring.append(item)
            _alog._next_id = self._saved_next


class TestBuildCatalog(_BaseRingTest):
    def test_empty_ring_returns_full_static(self):
        cat = build_catalog()
        self.assertEqual(cat["count"], len(_STATIC_AGENT_SKILLS))
        self.assertEqual(cat["static_count"], len(_STATIC_AGENT_SKILLS))
        self.assertEqual(cat["observed_count"], 0)
        names = {it["name"] for it in cat["items"]}
        self.assertEqual(names, set(_STATIC_AGENT_SKILLS))
        for it in cat["items"]:
            self.assertEqual(it["source"], "static")
            self.assertEqual(it["uses"], 0)
            self.assertIsNone(it["last_ts"])

    def test_observed_overrides_static_with_usage_stats(self):
        # web_search is in the static list; fire it via the ring.
        _alog.record_call("web_search", {"q": "weather"})
        cat = build_catalog()
        item = next(it for it in cat["items"] if it["name"] == "web_search")
        self.assertEqual(item["source"], "static")  # still static-classified
        self.assertEqual(item["uses"], 1)
        self.assertIsNotNone(item["last_ts"])
        # No duplicate entry in the observed section.
        web_count = sum(1 for it in cat["items"] if it["name"] == "web_search")
        self.assertEqual(web_count, 1)

    def test_unknown_tool_surfaces_as_observed(self):
        _alog.record_call("skill_xyz", {"a": 1})
        cat = build_catalog()
        # Static tools are still all there.
        self.assertEqual(cat["static_count"], len(_STATIC_AGENT_SKILLS))
        # And the new observed one too.
        self.assertEqual(cat["observed_count"], 1)
        skill = next(it for it in cat["items"] if it["name"] == "skill_xyz")
        self.assertEqual(skill["source"], "observed")
        self.assertEqual(skill["uses"], 1)

    def test_multiple_calls_increment_uses(self):
        for i in range(3):
            _alog.record_call("bash", {"cmd": f"ls {i}"})
        cat = build_catalog()
        item = next(it for it in cat["items"] if it["name"] == "bash")
        self.assertEqual(item["uses"], 3)

    def test_observed_extras_sorted_recent_first(self):
        # Two observed tools at different times.  Need to bypass the
        # auto-ts-on-record by mutating the ring directly so we can
        # control the ordering deterministically.
        with _alog._lock:
            _alog._ring.append({
                "id": 1, "ts": 1000, "tool": "skill_old", "args": {},
                "status": "done", "result": None, "execution_ms": None,
            })
            _alog._ring.append({
                "id": 2, "ts": 2000, "tool": "skill_new", "args": {},
                "status": "done", "result": None, "execution_ms": None,
            })
        cat = build_catalog()
        observed = [it for it in cat["items"] if it["source"] == "observed"]
        self.assertEqual([it["name"] for it in observed], ["skill_new", "skill_old"])

    def test_malformed_ring_entries_silently_skipped(self):
        with _alog._lock:
            _alog._ring.append({"id": 1, "ts": 1000, "tool": None, "args": {}})
            _alog._ring.append({"id": 2, "ts": 1000, "tool": "", "args": {}})
            _alog._ring.append({"id": 3, "ts": 1000, "tool": "fine", "args": {}})
        cat = build_catalog()
        names = {it["name"] for it in cat["items"]}
        self.assertIn("fine", names)
        # The malformed ones never surface as observed entries.
        observed = {it["name"] for it in cat["items"] if it["source"] == "observed"}
        self.assertEqual(observed, {"fine"})


class TestRouteRegistration(unittest.TestCase):
    def test_register_adds_get_route(self):
        app_mock = MagicMock()
        AgentSkillsRoutes().register(app_mock)
        # add_get called once with /api/v1/agent_skills as the path
        app_mock.router.add_get.assert_called_once()
        args, _kwargs = app_mock.router.add_get.call_args
        self.assertEqual(args[0], "/api/v1/agent_skills")


if __name__ == "__main__":
    unittest.main()
