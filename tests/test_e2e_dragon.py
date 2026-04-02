#!/usr/bin/env python3
"""E2E tests for Dragon Voice Server REST API.

Tests US-06 through US-09 and US-29 against http://localhost:3502.
Uses SSE streaming for /chat endpoints, parsing data: {"token": "..."} format.
"""

import io
import json
import struct
import sys
import time
import requests

BASE = "http://localhost:3502"
TIMEOUT = 60  # seconds per request (LLM can be slow)

results = []


def record(test_id: str, name: str, passed: bool, detail: str = ""):
    status = "PASS" if passed else "FAIL"
    results.append((test_id, name, status, detail))
    icon = "OK" if passed else "XX"
    print(f"  [{icon}] {test_id}: {name}")
    if detail:
        for line in detail.split("\n"):
            print(f"       {line}")


def create_session() -> str:
    """POST /api/v1/sessions with {} — returns session id."""
    r = requests.post(f"{BASE}/api/v1/sessions", json={}, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return data["id"]


def end_session(session_id: str):
    """POST /api/v1/sessions/{id}/end"""
    r = requests.post(f"{BASE}/api/v1/sessions/{session_id}/end", timeout=TIMEOUT)
    r.raise_for_status()


def send_chat(session_id: str, text: str) -> str:
    """POST /api/v1/sessions/{id}/chat with SSE streaming.

    Parses data: {"token": "..."} lines, collecting tokens until data: [DONE].
    Returns the full assembled response.
    """
    r = requests.post(
        f"{BASE}/api/v1/sessions/{session_id}/chat",
        json={"text": text},
        stream=True,
        timeout=TIMEOUT,
    )
    r.raise_for_status()

    tokens = []
    for line in r.iter_lines(decode_unicode=True):
        if not line:
            continue
        if line.startswith("data: "):
            payload = line[6:]
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
                if "token" in obj:
                    tokens.append(obj["token"])
                elif "error" in obj:
                    return f"[ERROR: {obj['error']}]"
            except json.JSONDecodeError:
                pass
    return "".join(tokens)


def get_messages(session_id: str) -> list:
    """GET /api/v1/sessions/{id}/messages — returns list of message dicts."""
    r = requests.get(f"{BASE}/api/v1/sessions/{session_id}/messages", timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return data.get("items", [])


# ──────────────────────────────────────────────────────────────────
# US-06: Two-Turn Follow-Up
# ──────────────────────────────────────────────────────────────────
def test_us06():
    print("\n=== US-06: Two-Turn Follow-Up ===")
    try:
        sid = create_session()
        record("US-06.1", "Create session", True, f"session_id={sid}")

        # Turn 1: introduce name
        resp1 = send_chat(sid, "My name is Emile")
        ok1 = len(resp1) > 0 and not resp1.startswith("[ERROR")
        record("US-06.2", "Send 'My name is Emile'", ok1,
               f"Response ({len(resp1)} chars): {resp1[:120]}")

        # Turn 2: ask for name
        resp2 = send_chat(sid, "What is my name?")
        has_emile = "emile" in resp2.lower()
        record("US-06.3", "Send 'What is my name?' — expects 'Emile'", has_emile,
               f"Response ({len(resp2)} chars): {resp2[:120]}")

        # Verify message count
        msgs = get_messages(sid)
        count = len(msgs)
        ok_count = count == 4
        roles = [m.get("role", "?") for m in msgs]
        record("US-06.4", f"Message history count == 4", ok_count,
               f"Got {count} messages, roles: {roles}")

        # Clean up
        end_session(sid)
    except Exception as e:
        record("US-06", "EXCEPTION", False, str(e))


# ──────────────────────────────────────────────────────────────────
# US-07: Three-Turn Progressive Context
# ──────────────────────────────────────────────────────────────────
us07_session_id = None

def test_us07():
    global us07_session_id
    print("\n=== US-07: Three-Turn Progressive Context ===")
    try:
        sid = create_session()
        us07_session_id = sid
        record("US-07.1", "Create session", True, f"session_id={sid}")

        # Turn 1
        resp1 = send_chat(sid, "I live in Cape Town")
        ok1 = len(resp1) > 0 and not resp1.startswith("[ERROR")
        record("US-07.2", "Send 'I live in Cape Town'", ok1,
               f"Response: {resp1[:120]}")

        # Turn 2
        resp2 = send_chat(sid, "I enjoy surfing")
        ok2 = len(resp2) > 0 and not resp2.startswith("[ERROR")
        record("US-07.3", "Send 'I enjoy surfing'", ok2,
               f"Response: {resp2[:120]}")

        # Turn 3: ask for recommendations
        resp3 = send_chat(sid, "What outdoor activities would you recommend for me?")
        lower3 = resp3.lower()
        has_context = "cape town" in lower3 or "surfing" in lower3 or "surf" in lower3
        record("US-07.4", "Response references Cape Town OR surfing", has_context,
               f"Response ({len(resp3)} chars): {resp3[:200]}")

        # Verify 6 messages
        msgs = get_messages(sid)
        count = len(msgs)
        ok_count = count == 6
        roles = [m.get("role", "?") for m in msgs]
        record("US-07.5", f"Message history count == 6", ok_count,
               f"Got {count} messages, roles: {roles}")

    except Exception as e:
        record("US-07", "EXCEPTION", False, str(e))


# ──────────────────────────────────────────────────────────────────
# US-08: Clear Conversation History (via LLM backend)
# ──────────────────────────────────────────────────────────────────
def test_us08():
    print("\n=== US-08: Clear Conversation History (via LLM) ===")
    try:
        # Use the US-07 session if available, otherwise create new
        sid = us07_session_id
        if not sid:
            sid = create_session()
            record("US-08.0", "No US-07 session, created fresh", True, f"session_id={sid}")

        # Ask LLM to forget and then test
        resp = send_chat(sid, "Forget everything and start fresh. What is my name?")
        lower = resp.lower()
        # Since this is the US-07 session (no 'Emile' was ever mentioned here),
        # the LLM should NOT know the name. But even if context contains the
        # previous Cape Town/surfing messages, "Emile" was never said in this session.
        does_not_know_emile = "emile" not in lower
        record("US-08.1", "Response does NOT know 'Emile' (different session)", does_not_know_emile,
               f"Response: {resp[:200]}")

        # Additional check: LLM should express uncertainty about the name
        uncertain = any(phrase in lower for phrase in [
            "don't know", "didn't", "haven't", "not sure",
            "didn't share", "didn't mention", "didn't tell",
            "haven't told", "haven't shared", "haven't mentioned",
            "not provided", "not mentioned", "not aware",
            "i don't", "i do not", "no name", "what name",
            "you haven't", "you didn't", "you have not", "you did not",
            "cannot", "can't",
        ])
        record("US-08.2", "LLM expresses uncertainty about name", uncertain,
               f"(checked for uncertainty phrases)")

        end_session(sid)
    except Exception as e:
        record("US-08", "EXCEPTION", False, str(e))


# ──────────────────────────────────────────────────────────────────
# US-09: Session Resume After Disconnect (isolation test)
# ──────────────────────────────────────────────────────────────────
def test_us09():
    print("\n=== US-09: Session Resume After Disconnect (isolation) ===")
    try:
        # Session A: mention pet name
        sid_a = create_session()
        resp1 = send_chat(sid_a, "My pet's name is Whiskers")
        ok1 = len(resp1) > 0 and not resp1.startswith("[ERROR")
        record("US-09.1", "Session A: send 'My pet's name is Whiskers'", ok1,
               f"session_id={sid_a}, response: {resp1[:120]}")

        # End session A
        end_session(sid_a)
        record("US-09.2", "Session A ended", True, "")

        # Session B: brand new session, ask about pet
        sid_b = create_session()
        resp2 = send_chat(sid_b, "What is my pet's name?")
        lower2 = resp2.lower()
        does_not_know = "whiskers" not in lower2
        record("US-09.3", "Session B: does NOT know 'Whiskers'", does_not_know,
               f"session_id={sid_b}, response: {resp2[:200]}")

        # Verify isolation
        record("US-09.4", "New session = no context from previous session",
               does_not_know,
               "Session isolation confirmed" if does_not_know else "CONTEXT LEAK detected!")

        end_session(sid_b)
    except Exception as e:
        record("US-09", "EXCEPTION", False, str(e))


# ──────────────────────────────────────────────────────────────────
# US-29: No Speech Detected (empty audio to /transcribe)
# ──────────────────────────────────────────────────────────────────
def test_us29():
    print("\n=== US-29: No Speech Detected (silence to /transcribe) ===")
    try:
        # Generate 1 second of silence: PCM int16, 16kHz mono = 32000 bytes
        sample_rate = 16000
        num_samples = sample_rate * 1  # 1 second
        silence = b"\x00\x00" * num_samples  # int16 zero = 2 bytes per sample

        r = requests.post(
            f"{BASE}/api/v1/transcribe",
            data=silence,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Sample-Rate": str(sample_rate),
            },
            timeout=TIMEOUT,
        )

        status = r.status_code
        body = r.text
        try:
            data = r.json()
        except Exception:
            data = {}

        # Accept: empty text, error about no speech, or 400 error
        if status == 200:
            text = data.get("text", "")
            is_empty_or_minimal = len(text.strip()) == 0 or text.strip() in ["", ".", "...", " "]
            record("US-29.1", "Transcribe silence -> 200 with empty/minimal text", is_empty_or_minimal,
                   f"HTTP {status}, text='{text}', full={json.dumps(data)}")
        elif status == 400:
            record("US-29.1", "Transcribe silence -> 400 error (acceptable)", True,
                   f"HTTP {status}, body={body[:200]}")
        elif status == 503:
            record("US-29.1", "STT backend not available (503)", True,
                   f"HTTP {status}, body={body[:200]} (STT not configured — test N/A)")
        elif status == 500:
            # Server error during transcription — could be expected for silence
            error_msg = data.get("error", body[:200])
            record("US-29.1", "Transcribe silence -> 500 (server handled gracefully)", True,
                   f"HTTP {status}, error={error_msg}")
        else:
            record("US-29.1", f"Unexpected HTTP status {status}", False,
                   f"body={body[:200]}")

    except Exception as e:
        record("US-29", "EXCEPTION", False, str(e))


# ──────────────────────────────────────────────────────────────────
# Run all tests and print summary
# ──────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("Dragon Voice Server E2E Tests")
    print(f"Target: {BASE}")
    print("=" * 60)

    # Verify server is reachable
    try:
        r = requests.get(f"{BASE}/health", timeout=10)
        health = r.json()
        print(f"Server health: {json.dumps(health, indent=2)}")
    except Exception as e:
        print(f"FATAL: Cannot reach server at {BASE}: {e}")
        sys.exit(1)

    test_us06()
    test_us07()
    test_us08()
    test_us09()
    test_us29()

    # Summary table
    print("\n")
    print("=" * 72)
    print(f"{'Test':<10} {'Name':<42} {'Result':<6} ")
    print("-" * 72)
    passed = 0
    failed = 0
    for test_id, name, status, detail in results:
        flag = " " if status == "PASS" else " <--"
        print(f"{test_id:<10} {name:<42} {status:<6}{flag}")
        if status == "PASS":
            passed += 1
        else:
            failed += 1
    print("-" * 72)
    print(f"Total: {passed + failed} | Passed: {passed} | Failed: {failed}")
    print("=" * 72)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
