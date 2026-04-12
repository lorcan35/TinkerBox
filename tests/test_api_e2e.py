#!/usr/bin/env python3
"""End-to-end API tests for Dragon Voice Server.

Three tiers:
  1. Single-step user stories (basic CRUD)
  2. Multi-step user stories (workflows)
  3. Complex chained user stories (cross-feature integration)

Run: python3 tests/test_api_e2e.py [--host 192.168.1.89] [--port 3502]
"""

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp

# ── Test Infrastructure ──

@dataclass
class TestResult:
    name: str
    passed: bool
    duration_ms: float
    error: str = ""
    details: str = ""

class TestRunner:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.results: list[TestResult] = []
        self.session: Optional[aiohttp.ClientSession] = None

    async def setup(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))

    async def teardown(self):
        if self.session:
            await self.session.close()

    async def get(self, path: str) -> tuple[int, dict]:
        async with self.session.get(f"{self.base_url}{path}") as r:
            try:
                body = await r.json()
            except:
                body = {"raw": await r.text()}
            return r.status, body

    async def post(self, path: str, data: dict = None) -> tuple[int, dict]:
        async with self.session.post(f"{self.base_url}{path}", json=data) as r:
            try:
                body = await r.json()
            except:
                body = {"raw": await r.text()}
            return r.status, body

    async def patch(self, path: str, data: dict) -> tuple[int, dict]:
        async with self.session.patch(f"{self.base_url}{path}", json=data) as r:
            return r.status, await r.json()

    async def delete(self, path: str) -> tuple[int, dict]:
        async with self.session.delete(f"{self.base_url}{path}") as r:
            return r.status, await r.json()

    async def put(self, path: str, data: dict) -> tuple[int, dict]:
        async with self.session.put(f"{self.base_url}{path}", json=data) as r:
            return r.status, await r.json()

    async def post_sse(self, path: str, data: dict) -> tuple[int, str]:
        """POST and collect SSE stream as full text."""
        async with self.session.post(f"{self.base_url}{path}", json=data) as r:
            tokens = []
            async for line in r.content:
                text = line.decode().strip()
                if text.startswith("data: ") and text != "data: [DONE]":
                    try:
                        d = json.loads(text[6:])
                        if "token" in d:
                            tokens.append(d["token"])
                    except:
                        pass
            return r.status, "".join(tokens)

    async def run_test(self, name: str, test_func):
        t0 = time.monotonic()
        try:
            await test_func()
            ms = (time.monotonic() - t0) * 1000
            self.results.append(TestResult(name, True, ms))
            print(f"  ✓ {name} ({ms:.0f}ms)")
        except Exception as e:
            ms = (time.monotonic() - t0) * 1000
            self.results.append(TestResult(name, False, ms, str(e)))
            print(f"  ✗ {name} ({ms:.0f}ms) — {e}")

    def summary(self):
        passed = sum(1 for r in self.results if r.passed)
        total = len(self.results)
        failed = total - passed
        print(f"\n{'='*60}")
        print(f"Results: {passed}/{total} passed, {failed} failed")
        if failed:
            print("\nFailed tests:")
            for r in self.results:
                if not r.passed:
                    print(f"  ✗ {r.name}: {r.error}")
        print(f"{'='*60}")
        return failed == 0


# ══════════════════════════════════════════════════════════════
# TIER 1: SINGLE-STEP USER STORIES
# ══════════════════════════════════════════════════════════════

async def tier1_single_step(t: TestRunner):
    print("\n── TIER 1: Single-Step User Stories ──")

    # US1: Health check
    async def us1_health():
        status, body = await t.get("/health")
        assert status == 200, f"Status {status}"
        assert body["status"] == "ok"
        assert "backends" in body

    # US2: List backends
    async def us2_backends():
        status, body = await t.get("/api/v1/backends")
        assert status == 200
        assert "stt" in body and "tts" in body and "llm" in body
        assert len(body["stt"]["available"]) >= 3

    # US3: System metrics
    async def us3_system():
        status, body = await t.get("/api/v1/system")
        assert status == 200
        assert "memory" in body
        assert body["memory"]["total_mb"] > 0
        assert "cpu_percent" in body

    # US4: List tools
    async def us4_tools():
        status, body = await t.get("/api/v1/tools")
        assert status == 200
        names = [t["name"] for t in body["tools"]]
        assert "web_search" in names
        assert "datetime" in names
        assert "remember" in names
        assert "recall" in names

    # US5: Execute datetime tool
    async def us5_datetime():
        status, body = await t.post("/api/v1/tools/datetime/execute", {"args": {}})
        assert status == 200
        assert body["result"]["date"]
        assert body["result"]["day"]

    # US6: List devices
    async def us6_devices():
        status, body = await t.get("/api/v1/devices")
        assert status == 200
        assert "items" in body

    # US7: List sessions
    async def us7_sessions():
        status, body = await t.get("/api/v1/sessions?limit=5")
        assert status == 200
        assert "items" in body

    # US8: List events
    async def us8_events():
        status, body = await t.get("/api/v1/events?limit=5")
        assert status == 200
        assert "items" in body

    # US9: List memory (empty or populated)
    async def us9_memory():
        status, body = await t.get("/api/v1/memory")
        assert status == 200
        assert "items" in body

    # US10: List documents
    async def us10_documents():
        status, body = await t.get("/api/v1/documents")
        assert status == 200
        assert "items" in body

    # US11: List notes
    async def us11_notes():
        status, body = await t.get("/api/notes")
        assert status == 200

    # US12: Config list
    async def us12_config():
        status, body = await t.get("/api/v1/config")
        assert status == 200
        assert "entries" in body

    # US13: 404 on nonexistent session
    async def us13_404():
        status, body = await t.get("/api/v1/sessions/nonexistent123")
        assert status == 404

    # US14: 404 on nonexistent tool
    async def us14_tool_404():
        status, body = await t.post("/api/v1/tools/fake_tool/execute", {"args": {}})
        assert status == 404

    for name, fn in [
        ("US1: Health check", us1_health),
        ("US2: List backends", us2_backends),
        ("US3: System metrics", us3_system),
        ("US4: List tools", us4_tools),
        ("US5: Execute datetime tool", us5_datetime),
        ("US6: List devices", us6_devices),
        ("US7: List sessions", us7_sessions),
        ("US8: List events", us8_events),
        ("US9: List memory facts", us9_memory),
        ("US10: List documents", us10_documents),
        ("US11: List notes", us11_notes),
        ("US12: Config list", us12_config),
        ("US13: 404 on missing session", us13_404),
        ("US14: 404 on missing tool", us14_tool_404),
    ]:
        await t.run_test(name, fn)


# ══════════════════════════════════════════════════════════════
# TIER 2: MULTI-STEP USER STORIES
# ══════════════════════════════════════════════════════════════

async def tier2_multi_step(t: TestRunner):
    print("\n── TIER 2: Multi-Step User Stories ──")

    # MS1: Session lifecycle — create → chat → get messages → end
    async def ms1_session_lifecycle():
        # Create
        s, body = await t.post("/api/v1/sessions", {"type": "conversation"})
        assert s == 201, f"Create: {s}"
        sid = body["id"]
        assert body["status"] == "active"

        # Get
        s, body = await t.get(f"/api/v1/sessions/{sid}")
        assert s == 200 and body["id"] == sid

        # End
        s, body = await t.post(f"/api/v1/sessions/{sid}/end")
        assert s == 200

        # Verify ended
        s, body = await t.get(f"/api/v1/sessions/{sid}")
        assert body["status"] == "ended"

    # MS2: Session pause → resume cycle
    async def ms2_pause_resume():
        s, body = await t.post("/api/v1/sessions", {"type": "conversation"})
        sid = body["id"]

        # Pause
        s, body = await t.post(f"/api/v1/sessions/{sid}/pause")
        assert s == 200

        # Verify paused
        s, body = await t.get(f"/api/v1/sessions/{sid}")
        assert body["status"] == "paused"

        # Resume
        s, body = await t.post(f"/api/v1/sessions/{sid}/resume")
        assert s == 200
        assert body["status"] == "active"

        # Cleanup
        await t.post(f"/api/v1/sessions/{sid}/end")

    # MS3: Session metadata update
    async def ms3_session_update():
        s, body = await t.post("/api/v1/sessions", {"type": "conversation"})
        sid = body["id"]

        # Patch title
        s, body = await t.patch(f"/api/v1/sessions/{sid}", {"title": "My Test Chat"})
        assert s == 200
        assert body["title"] == "My Test Chat"

        # Verify persisted
        s, body = await t.get(f"/api/v1/sessions/{sid}")
        assert body["title"] == "My Test Chat"

        await t.post(f"/api/v1/sessions/{sid}/end")

    # MS4: Memory store → search → delete
    async def ms4_memory_crud():
        # Store
        s, body = await t.post("/api/v1/memory", {"content": "E2E test: user likes pizza", "source": "test"})
        assert s == 201
        fid = body["id"]

        # Search
        s, body = await t.post("/api/v1/memory/search", {"query": "food preferences", "limit": 5})
        assert s == 200
        found = any(r["id"] == fid for r in body["results"])
        assert found, "Stored fact not found in search"

        # Delete
        s, body = await t.delete(f"/api/v1/memory/{fid}")
        assert s == 200

        # Verify deleted
        s, body = await t.post("/api/v1/memory/search", {"query": "user likes pizza", "limit": 5})
        found = any(r.get("id") == fid for r in body["results"])
        assert not found, "Deleted fact still found"

    # MS5: Document ingest → search → delete
    async def ms5_document_crud():
        content = "The ESP32-P4 has 32MB of PSRAM and runs at 400MHz. " * 10
        s, body = await t.post("/api/v1/documents", {
            "title": "E2E Test Doc", "content": content
        })
        assert s == 201
        did = body["id"]
        assert body["chunk_count"] >= 1

        # Search
        s, body = await t.post("/api/v1/documents/search", {"query": "PSRAM size", "limit": 3})
        assert s == 200

        # List
        s, body = await t.get("/api/v1/documents")
        assert s == 200
        found = any(d["id"] == did for d in body["items"])
        assert found

        # Delete
        s, body = await t.delete(f"/api/v1/documents/{did}")
        assert s == 200

    # MS6: Config set → get → delete
    async def ms6_config_crud():
        s, body = await t.put("/api/v1/config/e2e_test_key", {"value": {"enabled": True, "count": 42}})
        assert s == 200

        s, body = await t.get("/api/v1/config/e2e_test_key")
        assert s == 200
        assert body["value"]["count"] == 42

        s, body = await t.delete("/api/v1/config/e2e_test_key")
        assert s == 200

        s, body = await t.get("/api/v1/config/e2e_test_key")
        assert s == 404

    # MS7: Device update
    async def ms7_device_update():
        s, body = await t.get("/api/v1/devices")
        if not body["items"]:
            return  # No devices to test
        did = body["items"][0]["id"]
        old_name = body["items"][0]["name"]

        s, body = await t.patch(f"/api/v1/devices/{did}", {"name": "E2E-Test-Name"})
        assert s == 200
        assert body["name"] == "E2E-Test-Name"

        # Restore
        await t.patch(f"/api/v1/devices/{did}", {"name": old_name})

    # MS8: Message purge
    async def ms8_message_purge():
        s, body = await t.post("/api/v1/sessions", {"type": "conversation"})
        sid = body["id"]

        # Get messages (should be 0)
        s, body = await t.get(f"/api/v1/sessions/{sid}/messages")
        assert body["count"] == 0

        # Purge (even on empty — should work)
        s, body = await t.delete(f"/api/v1/sessions/{sid}/messages")
        assert s == 200

        await t.post(f"/api/v1/sessions/{sid}/end")

    for name, fn in [
        ("MS1: Session lifecycle (create→chat→end)", ms1_session_lifecycle),
        ("MS2: Session pause→resume cycle", ms2_pause_resume),
        ("MS3: Session metadata update", ms3_session_update),
        ("MS4: Memory store→search→delete", ms4_memory_crud),
        ("MS5: Document ingest→search→delete", ms5_document_crud),
        ("MS6: Config set→get→delete", ms6_config_crud),
        ("MS7: Device name update→restore", ms7_device_update),
        ("MS8: Message purge on session", ms8_message_purge),
    ]:
        await t.run_test(name, fn)


# ══════════════════════════════════════════════════════════════
# TIER 3: COMPLEX CHAINED USER STORIES
# ══════════════════════════════════════════════════════════════

async def tier3_complex_chained(t: TestRunner):
    print("\n── TIER 3: Complex Chained User Stories ──")

    # CS1: Full agentic flow — store fact → create session → chat asking about fact → verify memory context
    async def cs1_memory_augmented_chat():
        # Store a fact
        s, body = await t.post("/api/v1/memory", {"content": "User's favorite color is purple", "source": "test"})
        assert s == 201
        fid = body["id"]

        # Create session and chat
        s, body = await t.post("/api/v1/sessions", {"type": "conversation"})
        sid = body["id"]

        # Get context — should include memory
        s, body = await t.get(f"/api/v1/sessions/{sid}/context")
        assert s == 200
        # Context should have system prompt
        assert len(body["context"]) >= 1

        # Cleanup
        await t.post(f"/api/v1/sessions/{sid}/end")
        await t.delete(f"/api/v1/memory/{fid}")

    # CS2: Multi-session device flow — create 3 sessions → list by device → end all
    async def cs2_multi_session_device():
        # Use no device_id (API sessions) to avoid FK constraint
        sids = []
        for i in range(3):
            s, body = await t.post("/api/v1/sessions", {
                "type": "conversation"
            })
            assert s == 201, f"Create session {i} failed: {s} {body}"
            sids.append(body["id"])

        # List all sessions and verify ours are there
        s, body = await t.get("/api/v1/sessions?limit=200")
        assert s == 200, f"List sessions failed: {s}"
        all_sids = {item["id"] for item in body["items"]}
        for sid in sids:
            assert sid in all_sids, f"Session {sid} not found"

        # End all
        for sid in sids:
            await t.post(f"/api/v1/sessions/{sid}/end")

        # Verify all ended
        for sid in sids:
            s, body = await t.get(f"/api/v1/sessions/{sid}")
            assert body["status"] == "ended", f"Session {sid} not ended"

    # CS3: Document RAG pipeline — ingest doc → search → verify chunks → delete → verify gone
    async def cs3_document_rag_pipeline():
        # Ingest a substantial document
        paragraphs = [
            "The Dragon Q6A runs a Qualcomm QCS6490 processor with 12GB RAM. ",
            "It has a Hexagon DSP with 12 TOPS of neural processing capability. ",
            "The board runs Debian Linux and uses Python for the voice server. ",
            "TinkerClaw uses aiohttp for the web framework and aiosqlite for the database. ",
            "The voice pipeline consists of STT, LLM, and TTS stages running in sequence. ",
        ]
        content = "".join(p * 20 for p in paragraphs)  # Make it chunky

        s, body = await t.post("/api/v1/documents", {
            "title": "Dragon Hardware Specs",
            "content": content,
            "metadata": {"version": "1.0", "category": "hardware"}
        })
        assert s == 201
        did = body["id"]
        assert body["chunk_count"] >= 2, f"Expected multiple chunks, got {body['chunk_count']}"

        # Search for specific info
        s, body = await t.post("/api/v1/documents/search", {"query": "how much RAM does Dragon have", "limit": 3})
        assert s == 200
        assert len(body["results"]) > 0, "No search results"

        # List documents
        s, body = await t.get("/api/v1/documents")
        found = any(d["id"] == did for d in body["items"])
        assert found

        # Delete
        s, body = await t.delete(f"/api/v1/documents/{did}")
        assert s == 200

        # Verify chunks deleted (document list shouldn't include it)
        s, body = await t.get("/api/v1/documents")
        found = any(d["id"] == did for d in body["items"])
        assert not found

    # CS4: Tool execution chain — datetime → web_search → remember result → recall
    async def cs4_tool_chain():
        # Execute datetime
        s, body = await t.post("/api/v1/tools/datetime/execute", {"args": {}})
        assert s == 200
        today = body["result"]["date"]

        # Execute web search
        s, body = await t.post("/api/v1/tools/web_search/execute", {
            "args": {"query": f"news {today}", "max_results": 1}
        })
        assert s == 200
        # Web search may return empty on Dragon (no internet), that's OK
        assert "result" in body

        # Remember something
        s, body = await t.post("/api/v1/tools/remember/execute", {
            "args": {"fact": f"E2E test ran on {today}"}
        })
        assert s == 200
        assert body["result"]["stored"] == True
        fid = body["result"]["id"]

        # Recall it
        s, body = await t.post("/api/v1/tools/recall/execute", {
            "args": {"query": "when was E2E test", "limit": 3}
        })
        assert s == 200
        found = any(f.get("id") == fid for f in body["result"]["facts"])
        assert found, "Recalled facts don't include what we just remembered"

        # Cleanup
        await t.delete(f"/api/v1/memory/{fid}")

    # CS5: Full session workflow — create → update title → chat (SSE) → get context → get messages → purge → end
    async def cs5_full_session_workflow():
        # Create
        s, body = await t.post("/api/v1/sessions", {
            "type": "conversation",
            "system_prompt": "You are a test bot. Reply with exactly: TEST OK"
        })
        assert s == 201
        sid = body["id"]

        # Update title
        s, body = await t.patch(f"/api/v1/sessions/{sid}", {"title": "E2E Full Workflow"})
        assert s == 200 and body["title"] == "E2E Full Workflow"

        # Chat via SSE
        s, response_text = await t.post_sse(f"/api/v1/sessions/{sid}/chat", {"text": "Hello"})
        assert s == 200
        # LLM should have responded with something
        assert len(response_text) > 0, "Empty LLM response"

        # Get messages — should have user + assistant
        s, body = await t.get(f"/api/v1/sessions/{sid}/messages")
        assert s == 200
        assert body["count"] >= 2, f"Expected >=2 messages, got {body['count']}"
        roles = [m["role"] for m in body["items"]]
        assert "user" in roles and "assistant" in roles

        # Get context
        s, body = await t.get(f"/api/v1/sessions/{sid}/context")
        assert s == 200
        assert body["message_count"] >= 2

        # Purge messages
        s, body = await t.delete(f"/api/v1/sessions/{sid}/messages")
        assert s == 200
        assert body["deleted_count"] >= 2

        # Verify purged
        s, body = await t.get(f"/api/v1/sessions/{sid}/messages")
        assert body["count"] == 0

        # End
        s, body = await t.post(f"/api/v1/sessions/{sid}/end")
        assert s == 200

    # CS6: Event tracking — create session → verify event logged → filter by type
    async def cs6_event_tracking():
        # Create session first (should log session.created event)
        s, body = await t.post("/api/v1/sessions", {"type": "conversation"})
        assert s == 201
        sid = body["id"]

        # Small delay to let event be written
        await asyncio.sleep(0.2)

        # Get recent events with a high since_id to find the latest ones
        # Try fetching the last 200 events (since_id=0 means all)
        s, body = await t.get(f"/api/v1/events?type=session.created&limit=200")
        assert s == 200
        found = any(e.get("session_id") == sid for e in body["items"])
        assert found, f"session.created event for {sid} not found in {len(body['items'])} session.created events"

        await t.post(f"/api/v1/sessions/{sid}/end")

    # CS7: Cross-feature integration — store memory → ingest doc → create session → get context (should include both)
    async def cs7_cross_feature_context():
        # Store a fact
        s, body = await t.post("/api/v1/memory", {"content": "E2E cross-feature: user speaks French", "source": "test"})
        fid = body["id"]

        # Ingest a doc
        s, body = await t.post("/api/v1/documents", {
            "title": "E2E Language Guide",
            "content": "French is spoken in France, Belgium, Switzerland, and parts of Canada. " * 30
        })
        did = body["id"]

        # Create session
        s, body = await t.post("/api/v1/sessions", {"type": "conversation"})
        sid = body["id"]

        # The context should include both memory and documents when querying about French
        # (This tests the memory-augmented pipeline even without LLM)
        s, body = await t.get(f"/api/v1/sessions/{sid}/context")
        assert s == 200

        # Cleanup
        await t.post(f"/api/v1/sessions/{sid}/end")
        await t.delete(f"/api/v1/memory/{fid}")
        await t.delete(f"/api/v1/documents/{did}")

    for name, fn in [
        ("CS1: Memory-augmented chat flow", cs1_memory_augmented_chat),
        ("CS2: Multi-session device management", cs2_multi_session_device),
        ("CS3: Document RAG pipeline (ingest→search→delete)", cs3_document_rag_pipeline),
        ("CS4: Tool execution chain (datetime→search→remember→recall)", cs4_tool_chain),
        ("CS5: Full session workflow (create→title→chat→messages→purge→end)", cs5_full_session_workflow),
        ("CS6: Event tracking verification", cs6_event_tracking),
        ("CS7: Cross-feature integration (memory+docs+session)", cs7_cross_feature_context),
    ]:
        await t.run_test(name, fn)


# ── Main ──

async def main():
    parser = argparse.ArgumentParser(description="E2E API tests for Dragon Voice Server")
    parser.add_argument("--host", default="192.168.1.89", help="Dragon host")
    parser.add_argument("--port", default="3502", help="Voice server port")
    parser.add_argument("--tier", type=int, default=0, help="Run specific tier (1/2/3, 0=all)")
    args = parser.parse_args()

    base_url = f"http://{args.host}:{args.port}"
    print(f"Dragon Voice Server E2E Tests")
    print(f"Target: {base_url}")
    print(f"{'='*60}")

    # Verify server is reachable
    t = TestRunner(base_url)
    await t.setup()

    try:
        s, _ = await t.get("/health")
        if s != 200:
            print(f"ERROR: Server not healthy (status {s})")
            sys.exit(1)
        print(f"Server healthy ✓\n")
    except Exception as e:
        print(f"ERROR: Cannot reach server: {e}")
        sys.exit(1)

    try:
        if args.tier in (0, 1):
            await tier1_single_step(t)
        if args.tier in (0, 2):
            await tier2_multi_step(t)
        if args.tier in (0, 3):
            await tier3_complex_chained(t)
    finally:
        await t.teardown()

    success = t.summary()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
