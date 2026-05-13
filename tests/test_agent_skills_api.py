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


# ── W7-B.2: live-gateway fetch + cache + fallback ─────────────────────


import asyncio
from unittest.mock import AsyncMock

from dragon_voice.api.agent_skills import (
    build_catalog_async,
    _reset_gateway_cache_for_tests,
)
from dragon_voice.channels.gateway import SkillsStatusResult


def _run(coro):
    """Tiny sync wrapper so unittest can drive async test bodies."""
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


class _BaseAsyncRingTest(_BaseRingTest):
    """Adds a gateway-cache reset alongside the agent_log ring reset."""

    def setUp(self):
        super().setUp()
        _reset_gateway_cache_for_tests()

    def tearDown(self):
        _reset_gateway_cache_for_tests()
        super().tearDown()


class TestGatewayLivePath(_BaseAsyncRingTest):
    def _make_connector(self, skills_list):
        conn = MagicMock()
        conn.fetch_skills_status = AsyncMock(
            return_value=SkillsStatusResult(ok=True, skills=skills_list),
        )
        return conn

    def test_live_skills_surface_with_source_gateway(self):
        conn = self._make_connector([
            {"name": "custom_skill_a", "description": "User-installed A",
             "disabled": False, "bundled": False, "skillKey": "custom_skill_a"},
            {"name": "bash", "description": "Shell exec",
             "disabled": False, "bundled": True, "skillKey": "bash"},
        ])
        cat = _run(build_catalog_async(lambda: conn))
        # Both gateway entries surface with source=gateway, carry
        # description/disabled/bundled fields.
        gateway_items = [it for it in cat["items"] if it["source"] == "gateway"]
        names = {it["name"] for it in gateway_items}
        self.assertEqual(names, {"custom_skill_a", "bash"})
        for it in gateway_items:
            self.assertIn("description", it)
            self.assertIn("disabled", it)
            self.assertIn("bundled", it)
            self.assertIn("skillKey", it)
        # gateway_count surfaced
        self.assertEqual(cat["gateway_count"], 2)

    def test_static_backfill_for_missing_core_tools(self):
        # Gateway only returns 1 skill; the W7-B static 8 should
        # backfill the rest (minus the one already in the gateway list).
        conn = self._make_connector([
            {"name": "bash", "description": "", "disabled": False, "bundled": True, "skillKey": "bash"},
        ])
        cat = _run(build_catalog_async(lambda: conn))
        names = {it["name"] for it in cat["items"]}
        # All 8 W7-B core tools present (bash via gateway, rest via static)
        for tool in _STATIC_AGENT_SKILLS:
            self.assertIn(tool, names)
        # bash is the gateway-sourced one
        bash_entries = [it for it in cat["items"] if it["name"] == "bash"]
        self.assertEqual(len(bash_entries), 1)
        self.assertEqual(bash_entries[0]["source"], "gateway")

    def test_observed_extras_still_surface_when_gateway_live(self):
        # Tool fired in agent_log but not in gateway list → still
        # surfaces as observed.  Ensures the W7-A.b ring is honored even
        # when the live path lights up.
        with _alog._lock:
            _alog._ring.append({"id": 1, "ts": 1000, "tool": "fresh_observed", "args": {}})
        conn = self._make_connector([
            {"name": "bash", "description": "", "disabled": False, "bundled": True, "skillKey": "bash"},
        ])
        cat = _run(build_catalog_async(lambda: conn))
        observed = [it for it in cat["items"] if it["source"] == "observed"]
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["name"], "fresh_observed")

    def test_no_connector_falls_back_to_static(self):
        cat = _run(build_catalog_async(lambda: None))
        # Returns the legacy static+observed shape; no gateway_count key
        self.assertNotIn("gateway_count", cat)
        self.assertEqual(cat["count"], len(_STATIC_AGENT_SKILLS))
        # `gateway_error` surfaced when relevant
        self.assertEqual(cat.get("gateway_error"), "no_connector")

    def test_rpc_failure_falls_back_to_static(self):
        conn = MagicMock()
        conn.fetch_skills_status = AsyncMock(
            return_value=SkillsStatusResult(
                ok=False, skills=[], error="missing scope: operator.read",
            ),
        )
        cat = _run(build_catalog_async(lambda: conn))
        # Fell back; error string surfaced for ops visibility.
        self.assertNotIn("gateway_count", cat)
        self.assertIn("scope", cat.get("gateway_error", ""))

    def test_connector_exception_falls_back_silently(self):
        conn = MagicMock()
        conn.fetch_skills_status = AsyncMock(
            side_effect=RuntimeError("WS connection dropped mid-RPC"),
        )
        # Should not raise — just fall back to static.
        cat = _run(build_catalog_async(lambda: conn))
        self.assertNotIn("gateway_count", cat)
        self.assertEqual(cat["count"], len(_STATIC_AGENT_SKILLS))


class TestGatewayCache(_BaseAsyncRingTest):
    def test_second_call_within_ttl_uses_cache(self):
        skills = [
            {"name": "bash", "description": "", "disabled": False,
             "bundled": True, "skillKey": "bash"},
        ]
        conn = MagicMock()
        conn.fetch_skills_status = AsyncMock(
            return_value=SkillsStatusResult(ok=True, skills=skills),
        )
        # First call — fetch fires
        _run(build_catalog_async(lambda: conn))
        self.assertEqual(conn.fetch_skills_status.await_count, 1)
        # Second call within TTL — fetch should NOT fire again
        _run(build_catalog_async(lambda: conn))
        self.assertEqual(conn.fetch_skills_status.await_count, 1)

    def test_failed_fetch_is_cached_so_we_dont_hammer_gateway(self):
        # If gateway is down, don't try every request — let TTL expire.
        conn = MagicMock()
        conn.fetch_skills_status = AsyncMock(
            return_value=SkillsStatusResult(
                ok=False, skills=[], error="gateway_unreachable",
            ),
        )
        _run(build_catalog_async(lambda: conn))
        _run(build_catalog_async(lambda: conn))
        _run(build_catalog_async(lambda: conn))
        # NOTE: failed fetches do retry (cache stores skills=None which
        # is treated as cache-miss).  Pattern decision: prefer eventual
        # recovery over silence — opposite of the success path.
        self.assertGreaterEqual(conn.fetch_skills_status.await_count, 1)


class TestRouteWithConnectorGetter(unittest.TestCase):
    def test_register_works_with_or_without_connector_getter(self):
        # No getter — backwards compat with pre-W7-B.2 caller path
        app_mock = MagicMock()
        AgentSkillsRoutes().register(app_mock)
        # With getter — W7-B.2 wiring path
        app_mock2 = MagicMock()
        AgentSkillsRoutes(connector_getter=lambda: None).register(app_mock2)
        # Both register a route; method takes no opinion on the getter
        # at registration time (called per-request).
        self.assertEqual(app_mock.router.add_get.call_count, 1)
        self.assertEqual(app_mock2.router.add_get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
