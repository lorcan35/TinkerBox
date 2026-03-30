#!/usr/bin/env python3
"""Live multi-turn conversation test via WebSocket.

Connects to the running voice server, registers a device, sends two text
messages, and verifies the LLM remembers context from the first message.
Then checks the REST API to verify messages were persisted.

Usage: python3 tests/test_multiturn_live.py
"""

import asyncio
import json
import sys

import aiohttp

SERVER = "http://localhost:3502"
DEVICE_ID = "test-tab5-live"


async def collect_response(ws, timeout=60):
    """Collect LLM response tokens until streaming stops."""
    tokens = []
    try:
        while True:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=timeout)
            if msg["type"] == "llm":
                tokens.append(msg["text"])
            elif msg["type"] == "error":
                print(f"  ERROR: {msg.get('message', msg)}")
                break
            else:
                # Non-LLM event during streaming — if we have tokens, we're done
                if tokens:
                    break
    except asyncio.TimeoutError:
        if not tokens:
            print("  TIMEOUT waiting for LLM response")
    return "".join(tokens)


async def main():
    print("=== Multi-Turn Live Test ===\n")

    async with aiohttp.ClientSession() as session:
        # Connect WebSocket
        print("1. Connecting to WebSocket...")
        async with session.ws_connect(f"{SERVER}/ws/voice") as ws:

            # Register device
            print("2. Registering device...")
            await ws.send_json({
                "type": "register",
                "device_id": DEVICE_ID,
                "hardware_id": "AA:BB:CC:DD:EE:99",
                "name": "Live Test Tab5",
                "firmware_ver": "0.4.2",
                "platform": "esp32p4-tab5",
                "capabilities": {"mic": True, "speaker": True, "screen": True},
            })

            msg = await asyncio.wait_for(ws.receive_json(), timeout=10)
            assert msg["type"] == "session_start", f"Expected session_start, got {msg}"
            session_id = msg["session_id"]
            resumed = msg["resumed"]
            msg_count = msg["message_count"]
            print(f"   Session: {session_id} (resumed={resumed}, messages={msg_count})")

            # Turn 1: Introduce ourselves
            print("\n3. Turn 1: 'My name is Emile and I live in Dubai.'")
            await ws.send_json({"type": "text", "content": "My name is Emile and I live in Dubai."})
            r1 = await collect_response(ws)
            print(f"   Response: {r1[:200]}")

            # Turn 2: Ask about what we just said
            print("\n4. Turn 2: 'What is my name and where do I live?'")
            await ws.send_json({"type": "text", "content": "What is my name and where do I live?"})
            r2 = await collect_response(ws)
            print(f"   Response: {r2[:200]}")

            # Check if context was maintained
            r2_lower = r2.lower()
            has_name = "emile" in r2_lower
            has_city = "dubai" in r2_lower
            print(f"\n   Context check: name={'PASS' if has_name else 'FAIL'}, city={'PASS' if has_city else 'FAIL'}")

            # Turn 3: Third turn to test deeper context
            print("\n5. Turn 3: 'What have we talked about so far?'")
            await ws.send_json({"type": "text", "content": "What have we talked about so far?"})
            r3 = await collect_response(ws)
            print(f"   Response: {r3[:200]}")

            await ws.close()

        # Verify via REST API
        print("\n6. Verifying via REST API...")

        async with session.get(f"{SERVER}/api/v1/sessions/{session_id}") as resp:
            s = await resp.json()
            print(f"   Session status={s['status']}, message_count={s['message_count']}")

        async with session.get(f"{SERVER}/api/v1/sessions/{session_id}/messages") as resp:
            m = await resp.json()
            print(f"   Messages stored: {m['count']}")
            for msg in m["items"]:
                role = msg["role"]
                mode = msg.get("input_mode", "?")
                content = msg["content"][:80]
                print(f"     [{role}] ({mode}) {content}")

        async with session.get(f"{SERVER}/api/v1/devices") as resp:
            d = await resp.json()
            print(f"\n   Devices registered: {d['count']}")
            for dev in d["items"]:
                print(f"     {dev['id']} ({dev['name']}) online={dev.get('is_online', '?')}")

    # Verdict
    print("\n=== RESULTS ===")
    if has_name and has_city:
        print("PASS: LLM remembered context across turns!")
        return 0
    else:
        print("PARTIAL: LLM responded but may not have full context")
        print(f"  Name remembered: {has_name}")
        print(f"  City remembered: {has_city}")
        return 1


if __name__ == "__main__":
    rc = asyncio.run(main())
    sys.exit(rc)
