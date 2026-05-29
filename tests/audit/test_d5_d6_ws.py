"""Wave 6·A verification: D5 tool XML strip + D6 media event order.

Connects directly to Dragon's /ws/voice WebSocket (bypassing Tab5) and asserts
the outbound event sequence is correct for both audit items. Run manually
against a live Dragon — no fixtures, uses the real LLM and media pipeline.

Usage: python3 tests/audit/test_d5_d6_ws.py  (requires Dragon reachable at
DRAGON_HOST env var or default 192.168.1.91:3502).
"""

import asyncio
import json
import os
import secrets
import sys

import aiohttp
import pytest

DRAGON_URL = os.environ.get("DRAGON_URL", "ws://192.168.1.91:3502/ws/voice")

# These two functions are live-Dragon integration probes (see the module
# docstring), not self-contained unit tests — they open a real /ws/voice
# socket and exercise the live LLM + media pipeline.  Skip them in the normal
# suite; opt in with RUN_LIVE_AUDIT=1 against a reachable Dragon.  When opted
# in, run them as asyncio tests (the repo uses strict pytest-asyncio mode).
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("RUN_LIVE_AUDIT"),
        reason="live-Dragon audit probe; set RUN_LIVE_AUDIT=1 to run (see module docstring)",
    ),
]


async def _register_and_config(ws, model: str):
    did = "probe-" + secrets.token_hex(4)
    await ws.send_json({
        "type": "register",
        "device_id": did,
        "hardware_id": "probe-hw-" + secrets.token_hex(6),
        "session_id": "",
        "widget_capabilities": {"types": ["media", "card"]},
    })
    await ws.send_json({
        "type": "config_update",
        "voice_mode": 2,
        "llm_model": model,
    })
    await asyncio.sleep(3)


async def _collect(ws, until="tts_end", timeout=30):
    events = []
    llm_tokens = []
    text_update = None
    media = None
    try:
        async with asyncio.timeout(timeout):
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                m = json.loads(msg.data)
                t = m.get("type")
                events.append(t)
                if t == "llm":
                    llm_tokens.append(m.get("text", ""))
                elif t == "text_update":
                    text_update = m
                elif t == "media":
                    media = m
                if t == until:
                    break
    except asyncio.TimeoutError:
        pass
    return events, "".join(llm_tokens), text_update, media


async def test_d6_code_block():
    """D6: text_update must arrive BEFORE media event, with empty text so
    Tab5's ui_chat_update_last_message pops the raw markdown bubble."""
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(DRAGON_URL) as ws:
            await _register_and_config(ws, "openai/gpt-4o-mini")
            await ws.send_json({
                "type": "text",
                "content": 'return ```python\nprint("d6")\n``` exactly, nothing else',
            })
            events, llm_text, tu, media = await _collect(ws)

    assert tu is not None, f"text_update missing. events={events}"
    assert media is not None, f"media event missing. events={events}"
    tu_idx = events.index("text_update")
    m_idx = events.index("media")
    assert tu_idx < m_idx, f"text_update@{tu_idx} must come BEFORE media@{m_idx}"
    assert tu["text"] == "", f"text_update should be empty (pure code block), got {tu['text']!r}"
    print(f"[OK] D6 — text_update@{tu_idx} before media@{m_idx}, "
          f"text_update.text={tu['text']!r}, media.url={media.get('url','')[:60]}")


async def test_d5_tool_call_no_xml_leak():
    """D5: the `llm` token stream must not contain <tool>/<args> markup."""
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(DRAGON_URL) as ws:
            await _register_and_config(ws, "openai/gpt-4o-mini")
            await ws.send_json({
                "type": "text",
                "content": "what time is it? use the datetime tool",
            })
            events, llm_text, _, _ = await _collect(ws)

    assert "tool_call" in events, f"tool_call event missing. events={events}"
    leaked = any(m in llm_text for m in ("<tool>", "</tool>", "<args>", "</args>"))
    assert not leaked, f"tool XML leaked into llm stream: {llm_text!r}"
    print(f"[OK] D5 — tool_call fired, no XML in llm stream ({len(llm_text)} chars): "
          f"{llm_text[:80]!r}...")


async def main():
    print(f"Testing against {DRAGON_URL}")
    await test_d6_code_block()
    await test_d5_tool_call_no_xml_leak()
    print("\nAll audit checks PASS")


if __name__ == "__main__":
    asyncio.run(main())
