#!/usr/bin/env python3
"""Live session resume test.

Connect → register → send message → disconnect → reconnect → verify context preserved.

Usage: python3 tests/test_resume_live.py
"""

import asyncio
import sys

import aiohttp

SERVER = "http://localhost:3502"
DEVICE_ID = "resume-test-dev-2"
LLM_TIMEOUT = 120  # NPU cold start can take ~65s


async def collect_response(ws, timeout=LLM_TIMEOUT):
    tokens = []
    try:
        while True:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=timeout)
            if msg["type"] == "llm":
                tokens.append(msg["text"])
            elif msg["type"] == "error":
                print(f"  ERROR: {msg}")
                break
            elif tokens:
                break
    except asyncio.TimeoutError:
        if not tokens:
            print("  TIMEOUT waiting for response")
    return "".join(tokens)


async def main():
    print("=== Session Resume Test ===\n")
    session_id = None

    # --- Connection 1: establish session ---
    print("1. Connect #1: register + send message...")
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(f"{SERVER}/ws/voice") as ws:
            await ws.send_json({
                "type": "register",
                "device_id": DEVICE_ID,
                "hardware_id": "AA:BB:CC:DD:00:02",
                "name": "Resume Test v2",
                "firmware_ver": "0.4.2",
                "platform": "esp32p4-tab5",
                "capabilities": {"mic": True, "speaker": True},
            })
            msg = await asyncio.wait_for(ws.receive_json(), timeout=10)
            session_id = msg["session_id"]
            print(f"   Session: {session_id} (resumed={msg['resumed']}, msgs={msg['message_count']})")

            print("   Sending: 'The password is PINEAPPLE-42.'")
            await ws.send_json({"type": "text", "content": "The password is PINEAPPLE-42. Remember it."})
            r1 = await collect_response(ws)
            print(f"   Response: {r1[:150]}")
            await ws.close()

    print("\n2. Disconnected. Session should be paused. Waiting 3s...\n")
    await asyncio.sleep(3)

    # Verify session is paused via API
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{SERVER}/api/v1/sessions/{session_id}") as resp:
            sess = await resp.json()
            print(f"   Session status: {sess['status']}, messages: {sess['message_count']}")
            assert sess["status"] == "paused", f"Expected paused, got {sess['status']}"

    # --- Connection 2: resume session ---
    print("3. Connect #2: resume + ask about password...")
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(f"{SERVER}/ws/voice") as ws:
            await ws.send_json({
                "type": "register",
                "device_id": DEVICE_ID,
                "hardware_id": "AA:BB:CC:DD:00:02",
                "name": "Resume Test v2",
                "firmware_ver": "0.4.2",
                "platform": "esp32p4-tab5",
                "capabilities": {"mic": True, "speaker": True},
                "session_id": session_id,
            })
            msg = await asyncio.wait_for(ws.receive_json(), timeout=10)
            print(f"   Session: {msg['session_id']} (resumed={msg['resumed']}, msgs={msg['message_count']})")

            assert msg["session_id"] == session_id, "Session ID changed!"
            assert msg["resumed"] is True, "Not marked as resumed!"
            assert msg["message_count"] >= 2, f"Expected 2+ msgs, got {msg['message_count']}"

            print("   Sending: 'What is the password I told you?'")
            await ws.send_json({"type": "text", "content": "What is the password I told you?"})
            r2 = await collect_response(ws)
            print(f"   Response: {r2[:200]}")

            has_password = "pineapple" in r2.lower()
            print(f"\n   Password remembered after resume: {'PASS' if has_password else 'FAIL'}")
            await ws.close()

    # Final verification
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{SERVER}/api/v1/sessions/{session_id}/messages") as resp:
            m = await resp.json()
            print(f"\n4. Final message count: {m['count']}")
            for msg in m["items"]:
                print(f"   [{msg['role']}] {msg['content'][:80]}")

    print(f"\n=== RESULT: {'PASS' if has_password else 'FAIL'} ===")
    return 0 if has_password else 1


if __name__ == "__main__":
    rc = asyncio.run(main())
    sys.exit(rc)
