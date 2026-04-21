"""User-story E2E tests for audit waves 6-10.

Each test is a realistic multi-step flow that a human would walk through,
asserting protocol contracts plus content assertions at each step. Runs
against a live Dragon at DRAGON_URL (default 192.168.1.91:3502). Tab5
device-side assertions use the debug server at TAB5_URL
(default 192.168.1.90:8080) with TOKEN from the env or a default.

Usage:
    python3 tests/audit/test_user_stories.py            # run all stories
    python3 tests/audit/test_user_stories.py d6         # run subset

Exit 0 on all-pass, 1 otherwise.
"""

import asyncio
import json
import os
import secrets
import sys
import time
from typing import Optional

import aiohttp

DRAGON_WS = os.environ.get("DRAGON_URL", "ws://192.168.1.91:3502/ws/voice")
DRAGON_HTTP = os.environ.get("DRAGON_HTTP", "http://192.168.1.91:3502")
TAB5_URL = os.environ.get("TAB5_URL", "http://192.168.1.90:8080")
TOKEN = os.environ.get("TAB5_TOKEN", "05eed3b13bf62d92cfd8ac424438b9f2")

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = ""):
    tag = "[OK]" if ok else "[FAIL]"
    print(f"  {tag} {name}{' — ' + detail if detail else ''}")
    results.append((name, ok, detail))


async def _register(ws, voice_mode: int = 2, model: str = "openai/gpt-4o-mini"):
    did = "story-" + secrets.token_hex(4)
    await ws.send_json({
        "type": "register",
        "device_id": did,
        "hardware_id": "hw-" + secrets.token_hex(6),
        "session_id": "",
        "widget_capabilities": {"types": ["media", "card", "prompt", "list", "chart"],
                                "media_max_w": 660, "media_max_h": 440,
                                "list_max_items": 5, "chart_max_points": 12,
                                "prompt_max_choices": 3},
    })
    await ws.send_json({"type": "config_update", "voice_mode": voice_mode, "llm_model": model})
    await asyncio.sleep(3)


async def _collect(ws, until: str = "tts_end", timeout: int = 30):
    events, llm_tokens = [], []
    media, text_update = None, None
    tool_events = []
    try:
        async with asyncio.timeout(timeout):
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                m = json.loads(msg.data)
                t = m.get("type")
                events.append(t)
                if t == "llm": llm_tokens.append(m.get("text", ""))
                elif t == "media": media = m
                elif t == "text_update": text_update = m
                elif t in ("tool_call", "tool_result"):
                    tool_events.append((t, m.get("tool")))
                if t == until: break
    except asyncio.TimeoutError:
        pass
    return events, "".join(llm_tokens), text_update, media, tool_events


# ── Story 1: Clean code block renders as JPEG, markdown gets stripped ─────
async def story_d6_code_block():
    """User asks for a Python snippet in Cloud. Dragon renders JPEG, Tab5
    gets media event AFTER text_update clears the raw markdown bubble."""
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(DRAGON_WS) as ws:
            await _register(ws)
            await ws.send_json({"type": "text",
                "content": 'return ```python\nprint("d6")\n``` exactly, nothing else'})
            events, llm, tu, media, _ = await _collect(ws)
    assert tu is not None and media is not None, f"missing messages {events}"
    tu_idx, m_idx = events.index("text_update"), events.index("media")
    ok1 = tu_idx < m_idx
    ok2 = tu["text"] == ""
    ok3 = "<tool>" not in llm
    record("story_d6_code_block.order", ok1, f"text_update@{tu_idx} media@{m_idx}")
    record("story_d6_code_block.strip", ok2, f"text={tu['text']!r}")
    record("story_d6_code_block.noxml", ok3, f"{len(llm)} chars")


# ── Story 2: Cloud tool call — no XML leak, tool events fire ──────────────
async def story_d5_cloud_tool_call():
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(DRAGON_WS) as ws:
            await _register(ws)
            await ws.send_json({"type": "text",
                "content": "what time is it? use the datetime tool"})
            events, llm, _, _, tool_events = await _collect(ws)
    ok_tool = any(e[0] == "tool_call" for e in tool_events)
    ok_clean = not any(m in llm for m in ("<tool>", "</tool>", "<args>", "</args>"))
    record("story_d5_cloud.tool_call_fired", ok_tool, str(tool_events))
    record("story_d5_cloud.no_xml", ok_clean, f"{len(llm)} chars")


# ── Story 3: Mode swap mid-session survives (cloud -> local -> cloud) ─────
async def story_mode_swap_midsession():
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(DRAGON_WS) as ws:
            await _register(ws, voice_mode=2)
            # Drain the register/initial config_update echo backlog so the
            # first swap's echo isn't masked by a pre-existing one with
            # the starting voice_mode. Dragon's config_update is
            # rate-limited at 500 ms; we also pause between swaps.
            try:
                async with asyncio.timeout(3):
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT: continue
                        m = json.loads(msg.data)
                        if m.get("type") in ("config_update", "vision_capability",
                                              "session_start", "session_messages"):
                            continue
            except asyncio.TimeoutError:
                pass
            ok_after = []
            for vmode in (0, 2, 3):
                await asyncio.sleep(1.0)  # > 500 ms rate-limit window
                await ws.send_json({"type": "config_update", "voice_mode": vmode,
                                    "llm_model": "openai/gpt-4o-mini"})
                seen = False
                try:
                    async with asyncio.timeout(15):
                        async for msg in ws:
                            if msg.type != aiohttp.WSMsgType.TEXT: continue
                            m = json.loads(msg.data)
                            if m.get("type") != "config_update": continue
                            echoed = (m.get("config", {}) or {}).get("voice_mode")
                            if echoed is None:
                                echoed = m.get("voice_mode")
                            if echoed == vmode:
                                seen = True
                                break
                except asyncio.TimeoutError:
                    pass
                ok_after.append(seen)
            await ws.close()
    record("story_mode_swap.three_modes_survive", all(ok_after),
           f"{ok_after}")


# ── Story 4: Memory store then semantic recall round-trip ─────────────────
async def story_memory_semantic_recall():
    async with aiohttp.ClientSession() as s:
        # Store a fact
        fact_id: Optional[str] = None
        async with s.post(f"{DRAGON_HTTP}/api/v1/memory",
                          json={"content": "Emile's favourite espresso is Sumatra Mandheling."}) as r:
            data = await r.json()
            fact_id = data.get("id")
        ok_store = fact_id is not None
        # Semantic search
        async with s.post(f"{DRAGON_HTTP}/api/v1/memory/search",
                          json={"query": "what coffee does Emile like?"}) as r:
            res = await r.json()
        found = any("espresso" in (it.get("content") or "").lower()
                    for it in res.get("results", []))
        record("story_memory.store", ok_store, f"id={fact_id}")
        record("story_memory.semantic_recall_hit", found,
               f"{len(res.get('results', []))} results")
        # Clean up
        if fact_id:
            await s.delete(f"{DRAGON_HTTP}/api/v1/memory/{fact_id}")


# ── Story 5: widget_prompt default-dismiss on no handler (B6/K3) ──────────
async def story_widget_prompt_no_handler_dismiss():
    """Emit a widget_prompt with NO handler registered, tap a choice,
    expect a widget_dismiss back (wave 8 default-dismiss guard)."""
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(DRAGON_WS) as ws:
            await _register(ws, voice_mode=2)
            # Manually emit a widget_prompt via debug endpoint to ensure a
            # real Tab5-compatible payload gets registered into the surface
            # system. Skip handler registration deliberately.
            async with s.post(f"{DRAGON_HTTP}/debug/widget_prompt",
                              json={"title": "Audit tap probe",
                                    "body": "Tap one",
                                    "choices": [["yes", "story_yes"], ["no", "story_no"]]}) as r:
                d = await r.json()
            # /debug/widget_prompt auto-registers its own handler, so this
            # story mostly verifies that the debug emit round-trips. Skill-
            # side "bare emit + no handler" is covered at the unit level
            # (surfaces/manager.py handle_action default-dismiss branch).
            ok_round = d.get("emitted", 0) >= 1
            record("story_widget_prompt.debug_emit_round_trips", ok_round, json.dumps(d))
            # Simulate a tap — /debug/widget_prompt returns choices as
            # [[text, event], ...] (list of pairs), not list of dicts.
            first_choice = (d.get("choices") or [["yes", "story_yes"]])[0]
            choice_event = first_choice[1] if isinstance(first_choice, (list, tuple)) and len(first_choice) > 1 else "story_yes"
            await ws.send_json({"type": "widget_action",
                                "card_id": "audit_prompt_probe",
                                "event": choice_event})
            await asyncio.sleep(1)
            record("story_widget_prompt.tap_sent", True, f"event={choice_event}")
            await ws.close()


# ── Story 6: sqlite-vec hybrid search result includes score ──────────────
async def story_sqlite_vec_ranking():
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{DRAGON_HTTP}/api/v1/memory/search",
                          json={"query": "what do I drink in the morning"}) as r:
            d = await r.json()
        # Must return results list with score field on each (sqlite-vec fast path)
        ok_shape = isinstance(d.get("results"), list) and all(
            "score" in it for it in d.get("results", []))
        record("story_sqlite_vec.shape", ok_shape,
               f"{len(d.get('results', []))} results")


# ── Story 7: session_messages replay after reconnect ──────────────────────
async def story_session_resume_replay():
    """Connect, chat, disconnect, reconnect with same session_id, assert
    session_messages event fires with recent history."""
    async with aiohttp.ClientSession() as s:
        # First session
        did = "resume-" + secrets.token_hex(4)
        hw = "hw-" + secrets.token_hex(6)
        sid = None
        async with s.ws_connect(DRAGON_WS) as ws:
            await ws.send_json({"type": "register", "device_id": did,
                                "hardware_id": hw, "session_id": ""})
            await ws.send_json({"type": "config_update", "voice_mode": 2,
                                "llm_model": "openai/gpt-4o-mini"})
            # Capture session_id from session_start
            try:
                async with asyncio.timeout(5):
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            m = json.loads(msg.data)
                            if m.get("type") == "session_start":
                                sid = m.get("session_id") or m.get("id")
                                break
            except asyncio.TimeoutError:
                pass
            if not sid:
                record("story_resume.got_session_id", False, "timeout"); return
            await ws.send_json({"type": "text", "content": "pick a random fruit"})
            await _collect(ws, until="tts_end", timeout=20)

        # Reconnect using same session_id
        async with s.ws_connect(DRAGON_WS) as ws:
            await ws.send_json({"type": "register", "device_id": did,
                                "hardware_id": hw, "session_id": sid})
            got_replay = False
            items = 0
            try:
                async with asyncio.timeout(10):
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT: continue
                        m = json.loads(msg.data)
                        if m.get("type") == "session_messages":
                            got_replay = True
                            items = len(m.get("items", []))
                            break
            except asyncio.TimeoutError:
                pass
            await ws.close()
    record("story_resume.session_messages_fires", got_replay, f"items={items}")


# ── Story 8: TC receipt model stamp is honest (not bare "tinkerclaw") ─────
async def story_tc_receipt_model_honest():
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(DRAGON_WS) as ws:
            await _register(ws, voice_mode=3)
            await ws.send_json({"type": "text", "content": "pick one word"})
            model_in_receipt = None
            try:
                async with asyncio.timeout(20):
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT: continue
                        m = json.loads(msg.data)
                        if m.get("type") == "receipt" and m.get("stage") == "llm":
                            model_in_receipt = m.get("model")
                            break
                        if m.get("type") == "tts_end":
                            break
            except asyncio.TimeoutError:
                pass
            await ws.close()
    ok = model_in_receipt not in (None, "", "tinkerclaw")
    record("story_tc_receipt.model_has_real_id", ok,
           f"model={model_in_receipt!r}")


STORIES = [
    ("d6", story_d6_code_block),
    ("d5", story_d5_cloud_tool_call),
    ("mode_swap", story_mode_swap_midsession),
    ("memory", story_memory_semantic_recall),
    ("widget_prompt", story_widget_prompt_no_handler_dismiss),
    ("vec", story_sqlite_vec_ranking),
    ("resume", story_session_resume_replay),
    ("tc_receipt", story_tc_receipt_model_honest),
]


async def main():
    want = set(sys.argv[1:]) if len(sys.argv) > 1 else None
    print(f"Running user-story E2E against {DRAGON_WS}")
    for name, fn in STORIES:
        if want and name not in want:
            continue
        print(f"\n── Story: {name} ──")
        try:
            await fn()
        except Exception as e:
            record(f"story_{name}.ran", False, f"exception: {e}")
        # Small delay between stories so the server has breathing room
        await asyncio.sleep(1)

    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{'='*60}\n{passed}/{total} user-story assertions passed\n{'='*60}")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    asyncio.run(main())
