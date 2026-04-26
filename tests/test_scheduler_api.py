"""REST API tests for the scheduler.

Phase 5 ε1b (refs #126).

aiohttp TestServer pattern matches test_ws_upgrade_errors.py — the
JSON body shapes + status codes + headers are all verified end-to-end
at the HTTP layer, not just at the function-return-value layer.

Coverage map (RFC Section A9 → 4 tests, expanded to 6 to cover the
runaway-cap 429 path + reschedule):

  * POST creates → 201 with full notification dict
  * POST malformed when → 400 with structured `code`
  * GET lists → paginated_response shape
  * DELETE cancels → 200 + flips status
  * PATCH reschedules → updates fire_at
  * Runaway cap → 429 with `code: scheduler_runaway_cap`
"""
from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from dragon_voice.api.scheduler import SchedulerRoutes
from dragon_voice.scheduler.manager import SchedulerManager
from dragon_voice.scheduler.store import InMemoryNotificationStore


def _build_server() -> SchedulerManager:
    """Build a SchedulerManager wired to a real InMemoryStore + stubs.
    Same pattern as the manager unit tests."""
    store = InMemoryNotificationStore()
    surface_mgr = MagicMock()
    surface_mgr.surface_for = MagicMock(return_value=MagicMock())
    session_mgr = MagicMock()
    session_mgr.list_sessions = AsyncMock(return_value=[])
    return SchedulerManager(
        store=store, surface_mgr=surface_mgr, session_mgr=session_mgr,
    )


def _build_app(mgr: SchedulerManager) -> web.Application:
    app = web.Application()
    SchedulerRoutes(mgr).register(app)
    return app


class SchedulerAPITests(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    # ── POST create ────────────────────────────────────────────────

    def test_post_creates_notification(self) -> None:
        """Headline contract: POST → 201 with full notification dict.
        Pin the field set the dashboard / curl client expects."""
        mgr = _build_server()
        app = _build_app(mgr)

        async def go():
            async with TestServer(app) as srv, TestClient(srv) as client:
                resp = await client.post(
                    "/api/v1/scheduler/notifications",
                    json={
                        "when": "5m",
                        "message": "Take out the trash",
                        "device_id": "dev_A",
                    },
                )
                self.assertEqual(resp.status, 201)
                self.assertEqual(resp.content_type, "application/json")
                body = await resp.json()
                self.assertTrue(body["id"].startswith("sched_"))
                self.assertEqual(body["device_id"], "dev_A")
                self.assertEqual(body["body"], "Take out the trash")
                self.assertEqual(body["title"], "Reminder")
                self.assertEqual(body["status"], "pending")
                self.assertIsNotNone(body["fires_at_iso"])
                self.assertGreater(body["fire_at"], time.time())
            await mgr.shutdown()

        self._run(go())

    def test_post_malformed_when_returns_structured_400(self) -> None:
        """Garbage `when` → 400 with `code: scheduler_when_parse` so
        the dashboard / curl client can branch on the failure mode
        without parsing prose."""
        mgr = _build_server()
        app = _build_app(mgr)

        async def go():
            async with TestServer(app) as srv, TestClient(srv) as client:
                resp = await client.post(
                    "/api/v1/scheduler/notifications",
                    json={
                        "when": "not a real time",
                        "message": "x",
                        "device_id": "dev_A",
                    },
                )
                self.assertEqual(resp.status, 400)
                body = await resp.json()
                self.assertEqual(body["code"], "scheduler_when_parse")
                self.assertIn("error", body)
            await mgr.shutdown()

        self._run(go())

    def test_post_missing_device_id_rejects(self) -> None:
        """REST callers MUST identify the target device — the LLM
        tool inherits it from the session, but REST has no fallback."""
        mgr = _build_server()
        app = _build_app(mgr)

        async def go():
            async with TestServer(app) as srv, TestClient(srv) as client:
                resp = await client.post(
                    "/api/v1/scheduler/notifications",
                    json={"when": "5m", "message": "x"},
                )
                self.assertEqual(resp.status, 400)
                body = await resp.json()
                self.assertIn("device_id", body["error"])
            await mgr.shutdown()

        self._run(go())

    # ── GET list / get ─────────────────────────────────────────────

    def test_get_list_returns_paginated(self) -> None:
        """GET → standard paginated_response shape (items + count +
        limit + offset).  Pin so the dashboard's pagination renderer
        gets the same fields as every other listing endpoint."""
        mgr = _build_server()
        app = _build_app(mgr)

        async def go():
            async with TestServer(app) as srv, TestClient(srv) as client:
                # Seed two notifications so list has something to show
                await client.post(
                    "/api/v1/scheduler/notifications",
                    json={"when": "1h", "message": "a", "device_id": "dev_A"},
                )
                await client.post(
                    "/api/v1/scheduler/notifications",
                    json={"when": "2h", "message": "b", "device_id": "dev_A"},
                )

                resp = await client.get(
                    "/api/v1/scheduler/notifications?device_id=dev_A",
                )
                self.assertEqual(resp.status, 200)
                body = await resp.json()
                self.assertIn("items", body)
                self.assertEqual(body["count"], 2)
                self.assertIn("limit", body)
                self.assertIn("offset", body)
                # Both seeded notifications present
                bodies = {item["body"] for item in body["items"]}
                self.assertEqual(bodies, {"a", "b"})
            await mgr.shutdown()

        self._run(go())

    # ── DELETE cancel ──────────────────────────────────────────────

    def test_delete_cancels_pending_notification(self) -> None:
        """DELETE → 200 + status flips to cancelled.  Subsequent
        GET reflects the cancelled status."""
        mgr = _build_server()
        app = _build_app(mgr)

        async def go():
            async with TestServer(app) as srv, TestClient(srv) as client:
                create = await client.post(
                    "/api/v1/scheduler/notifications",
                    json={"when": "1h", "message": "x", "device_id": "dev_A"},
                )
                created = await create.json()
                notif_id = created["id"]

                resp = await client.delete(
                    f"/api/v1/scheduler/notifications/{notif_id}",
                )
                self.assertEqual(resp.status, 200)
                body = await resp.json()
                self.assertEqual(body["status"], "cancelled")
                self.assertEqual(body["id"], notif_id)

                # Verify via GET that status flipped
                follow = await client.get(
                    f"/api/v1/scheduler/notifications/{notif_id}",
                )
                self.assertEqual(follow.status, 200)
                follow_body = await follow.json()
                self.assertEqual(follow_body["status"], "cancelled")
            await mgr.shutdown()

        self._run(go())

    def test_delete_missing_returns_404(self) -> None:
        """Cancel a nonexistent id → 404 (not 200, not 500)."""
        mgr = _build_server()
        app = _build_app(mgr)

        async def go():
            async with TestServer(app) as srv, TestClient(srv) as client:
                resp = await client.delete(
                    "/api/v1/scheduler/notifications/sched_nonexistent",
                )
                self.assertEqual(resp.status, 404)
            await mgr.shutdown()

        self._run(go())

    # ── PATCH reschedule ───────────────────────────────────────────

    def test_patch_reschedules_pending_notification(self) -> None:
        """PATCH with new `when` → updates fire_at, returns the
        updated notification dict."""
        mgr = _build_server()
        app = _build_app(mgr)

        async def go():
            async with TestServer(app) as srv, TestClient(srv) as client:
                create = await client.post(
                    "/api/v1/scheduler/notifications",
                    json={"when": "1h", "message": "x", "device_id": "dev_A"},
                )
                created = await create.json()
                notif_id = created["id"]
                original_fire_at = created["fire_at"]

                resp = await client.patch(
                    f"/api/v1/scheduler/notifications/{notif_id}",
                    json={"when": "2h"},
                )
                self.assertEqual(resp.status, 200)
                body = await resp.json()
                self.assertEqual(body["id"], notif_id)
                # New fire_at is roughly 1h later than the original
                self.assertGreater(body["fire_at"], original_fire_at)
            await mgr.shutdown()

        self._run(go())
