"""Wave 12: regression tests for the cross-session agent-log ring + endpoint.

Covers:
  1. record_call appends a "running" entry with monotonic id
  2. record_result closes the matching call (newest-first match for
     same-tool-twice-in-one-turn)
  3. record_result without a matching call appends a synthetic done entry
  4. Result preview truncates long strings and renders dicts as JSON
  5. snapshot(since_id=N) returns only entries newer than N
  6. snapshot(limit=L) caps the response
  7. Ring is bounded — old entries evict at maxlen
  8. GET /api/v1/agent_log returns the ring + head_id/tail_id

Run:
    python3 -m pytest tests/test_agent_log.py -v
"""

from __future__ import annotations

import asyncio
import json
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from dragon_voice.api import agent_log


class TestRingMechanics(unittest.TestCase):
    def setUp(self) -> None:
        agent_log.reset_for_tests()

    def test_record_call_appends_running_entry(self) -> None:
        cid = agent_log.record_call("web_search", {"query": "esp32"})
        self.assertEqual(cid, 1)
        items = agent_log.snapshot()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], 1)
        self.assertEqual(items[0]["tool"], "web_search")
        self.assertEqual(items[0]["status"], "running")
        self.assertEqual(items[0]["args"], {"query": "esp32"})
        self.assertIsNone(items[0]["result"])
        self.assertIsNone(items[0]["execution_ms"])

    def test_ids_are_monotonic_and_unique(self) -> None:
        ids = [agent_log.record_call("x") for _ in range(5)]
        self.assertEqual(ids, [1, 2, 3, 4, 5])

    def test_record_result_closes_matching_call(self) -> None:
        agent_log.record_call("web_search", {"q": "abc"})
        agent_log.record_result("web_search", "result text", execution_ms=42)
        items = agent_log.snapshot()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "done")
        self.assertEqual(items[0]["result"], "result text")
        self.assertEqual(items[0]["execution_ms"], 42)

    def test_result_matches_newest_running_first(self) -> None:
        agent_log.record_call("web_search", {"q": "old"})
        agent_log.record_call("web_search", {"q": "new"})
        agent_log.record_result("web_search", "newer", execution_ms=1)
        items = agent_log.snapshot()  # newest-first ordering
        # The "new" call (id=2) is newer; it should be the one closed.
        new_entry = next(i for i in items if i["args"].get("q") == "new")
        old_entry = next(i for i in items if i["args"].get("q") == "old")
        self.assertEqual(new_entry["status"], "done")
        self.assertEqual(old_entry["status"], "running")

    def test_orphan_result_appends_synthetic_done(self) -> None:
        agent_log.record_result("calculator", {"value": 12}, execution_ms=3)
        items = agent_log.snapshot()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "done")
        self.assertEqual(items[0]["tool"], "calculator")
        self.assertIn("12", items[0]["result"])

    def test_long_result_truncates(self) -> None:
        agent_log.record_call("web_search")
        agent_log.record_result("web_search", "x" * 500)
        items = agent_log.snapshot()
        self.assertLessEqual(len(items[0]["result"]), 120)
        self.assertTrue(items[0]["result"].endswith("…"))

    def test_dict_result_renders_as_json(self) -> None:
        agent_log.record_call("recall")
        agent_log.record_result("recall", {"facts": ["alpha", "beta"]})
        items = agent_log.snapshot()
        self.assertIn("alpha", items[0]["result"])
        self.assertIn("beta", items[0]["result"])

    def test_snapshot_since_id_filters(self) -> None:
        agent_log.record_call("a")
        agent_log.record_call("b")
        agent_log.record_call("c")
        items = agent_log.snapshot(since_id=1)
        ids = sorted(i["id"] for i in items)
        self.assertEqual(ids, [2, 3])

    def test_snapshot_respects_limit(self) -> None:
        for i in range(10):
            agent_log.record_call(f"tool_{i}")
        items = agent_log.snapshot(limit=3)
        self.assertEqual(len(items), 3)

    def test_ring_evicts_oldest_at_maxlen(self) -> None:
        cap = agent_log._RING_SIZE
        for i in range(cap + 5):
            agent_log.record_call(f"t{i}")
        items = agent_log.snapshot(limit=cap)  # otherwise default limit=50 caps
        self.assertEqual(len(items), cap)
        # oldest (id=1..5) evicted; head_id should be cap+5, tail_id 6
        self.assertEqual(agent_log.head_id(), cap + 5)
        self.assertEqual(agent_log.tail_id(), 6)


class TestEndpoint(unittest.TestCase):
    def setUp(self) -> None:
        agent_log.reset_for_tests()

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_get_returns_ring_with_head_tail(self) -> None:
        agent_log.record_call("web_search", {"q": "esp32"})
        agent_log.record_result("web_search", "ok", execution_ms=10)
        agent_log.record_call("calculator", {"expr": "2+2"})

        async def go():
            app = web.Application()
            agent_log.AgentLogRoutes().register(app)
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/v1/agent_log")
                self.assertEqual(resp.status, 200)
                body = await resp.json()
                self.assertEqual(body["count"], 2)
                self.assertEqual(len(body["items"]), 2)
                # newest-first
                self.assertEqual(body["items"][0]["tool"], "calculator")
                self.assertEqual(body["items"][1]["tool"], "web_search")
                self.assertEqual(body["head_id"], 2)
                self.assertEqual(body["tail_id"], 1)

        self._run(go())

    def test_get_since_id_filters(self) -> None:
        agent_log.record_call("a")
        agent_log.record_call("b")
        agent_log.record_call("c")

        async def go():
            app = web.Application()
            agent_log.AgentLogRoutes().register(app)
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/v1/agent_log?since_id=2")
                body = await resp.json()
                self.assertEqual(body["count"], 1)
                self.assertEqual(body["items"][0]["tool"], "c")

        self._run(go())

    def test_get_caps_limit_at_ring_size(self) -> None:
        async def go():
            app = web.Application()
            agent_log.AgentLogRoutes().register(app)
            async with TestClient(TestServer(app)) as client:
                # Limit larger than ring should be capped silently
                resp = await client.get("/api/v1/agent_log?limit=99999")
                self.assertEqual(resp.status, 200)
                body = await resp.json()
                self.assertEqual(body["ring_size"], agent_log._RING_SIZE)

        self._run(go())


class TestSourceField(unittest.TestCase):
    """W7-A.3: every entry carries a source field so consumers can
    distinguish Dragon-side tools from gateway-routed ones."""

    def setUp(self) -> None:
        agent_log.reset_for_tests()

    def test_record_call_defaults_to_dragon(self):
        agent_log.record_call("web_search", {"q": "esp32"})
        items = agent_log.snapshot()
        self.assertEqual(items[0]["source"], "dragon")

    def test_record_call_accepts_gateway_source(self):
        agent_log.record_call("bash", {"cmd": "ls"}, source="gateway")
        items = agent_log.snapshot()
        self.assertEqual(items[0]["source"], "gateway")

    def test_empty_source_defaults_to_dragon(self):
        agent_log.record_call("x", source="")
        items = agent_log.snapshot()
        self.assertEqual(items[0]["source"], "dragon")

    def test_result_matches_within_source_scope(self):
        """Same tool name from different sources must not collide on
        result match.  Dragon's web_search and gateway's web_search
        are independent entries even if firing in the same window."""
        agent_log.record_call("web_search", {"q": "a"}, source="dragon")
        agent_log.record_call("web_search", {"q": "b"}, source="gateway")
        agent_log.record_result("web_search", "dragon-done", source="dragon")
        items = agent_log.snapshot()
        # newest-first, so [0] = gateway (still running), [1] = dragon (done)
        self.assertEqual(items[0]["source"], "gateway")
        self.assertEqual(items[0]["status"], "running")
        self.assertEqual(items[1]["source"], "dragon")
        self.assertEqual(items[1]["status"], "done")
        self.assertEqual(items[1]["result"], "dragon-done")

    def test_unknown_source_preserved(self):
        """API doesn't gatekeep source values — a future surface like
        'mcp' or 'skill_bridge' can self-identify without a code
        change."""
        agent_log.record_call("x", source="mcp")
        items = agent_log.snapshot()
        self.assertEqual(items[0]["source"], "mcp")

    def test_endpoint_returns_source_bucket_counts(self):
        """GET /api/v1/agent_log returns a `sources` dict with per-
        source counts (Dragon vs gateway vs others)."""
        agent_log.record_call("a", source="dragon")
        agent_log.record_call("b", source="dragon")
        agent_log.record_call("c", source="gateway")

        async def go():
            app = web.Application()
            agent_log.AgentLogRoutes().register(app)
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/v1/agent_log")
                self.assertEqual(resp.status, 200)
                body = await resp.json()
                self.assertIn("sources", body)
                self.assertEqual(body["sources"].get("dragon"), 2)
                self.assertEqual(body["sources"].get("gateway"), 1)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(go())
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main()
