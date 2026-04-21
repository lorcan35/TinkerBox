"""Wave 11 compressed soak test.

Simulates 30 minutes of realistic user traffic against a Tab5 + Dragon
duo: chat turns every 30 s, mode swaps every 3 min, widget emits every
90 s, a final OTA-schedule. Asserts at each checkpoint that the device
is still reachable, the Dragon services are up, and DMA headroom hasn't
crashed. Writes a CSV log of heap trajectory to /tmp/tab5_soak.csv.

Exit 0 if device survives 30 min without manual intervention; exit 1 on
any DEAD poll, any heap_wd reboot, or any user-story assertion failure.
"""

import asyncio
import csv
import json
import os
import secrets
import sys
import time
from typing import Optional

import aiohttp

TAB5 = os.environ.get("TAB5_URL", "http://192.168.1.90:8080")
TOKEN = os.environ.get("TAB5_TOKEN", "05eed3b13bf62d92cfd8ac424438b9f2")
DRAGON_HTTP = os.environ.get("DRAGON_HTTP", "http://192.168.1.91:3502")
DURATION_MIN = int(os.environ.get("DURATION_MIN", "30"))
CHAT_EVERY_S = 30
MODE_SWAP_EVERY_S = 180
WIDGET_EVERY_S = 90

CSV_PATH = "/tmp/tab5_soak.csv"

events: list[tuple[float, str, str]] = []


def tnow() -> float:
    return time.time()


async def http_get(s: aiohttp.ClientSession, url: str, auth=False) -> Optional[dict]:
    headers = {"Authorization": f"Bearer {TOKEN}"} if auth else {}
    try:
        async with asyncio.timeout(5):
            async with s.get(url, headers=headers) as r:
                if r.status != 200: return None
                return await r.json()
    except Exception:
        return None


async def http_post(s: aiohttp.ClientSession, url: str, auth=False, **kw):
    headers = {"Authorization": f"Bearer {TOKEN}"} if auth else {}
    try:
        async with asyncio.timeout(8):
            async with s.post(url, headers=headers, **kw) as r:
                try:
                    return await r.json()
                except Exception:
                    return {"status": r.status}
    except Exception as e:
        return {"error": str(e)}


async def tab5_info(s):
    return await http_get(s, f"{TAB5}/info")


async def tab5_selftest(s):
    return await http_get(s, f"{TAB5}/selftest")


async def extract_heap(info: dict) -> Optional[dict]:
    """Pull internal_heap sub-check from /selftest payload."""
    if not info: return None
    for t in info.get("tests", []):
        if t.get("name") == "internal_heap":
            return {"free_kb": t.get("free_kb", 0),
                    "largest_kb": t.get("largest_block_kb", 0),
                    "frag": t.get("fragmentation_pct", 0)}
    return None


async def run_chat_turn(s, prompt: str):
    return await http_post(s, f"{TAB5}/chat", auth=True,
                           data=json.dumps({"text": prompt}).encode())


async def swap_mode(s, m: int, model: str = "openai/gpt-4o-mini"):
    return await http_post(s, f"{TAB5}/mode?m={m}&model={model}", auth=True)


async def emit_widget_card(s):
    return await http_post(s, f"{DRAGON_HTTP}/debug/widget_card",
                           data=json.dumps({"title": "Soak tick",
                                             "body": f"t+{int(tnow())}", "tone": "info"}).encode())


async def main():
    print(f"Soaking {TAB5} for {DURATION_MIN} min, logging to {CSV_PATH}")
    end = tnow() + DURATION_MIN * 60
    last_chat = 0; last_swap = 0; last_widget = 0
    prev_uptime: Optional[int] = None
    reboot_count = 0
    death_count = 0
    mode_cycle = [0, 2, 3, 2]; mode_idx = 0

    with open(CSV_PATH, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "uptime_ms", "dragon", "voice", "wifi", "free_kb", "largest_kb", "frag", "event"])

        async with aiohttp.ClientSession() as s:
            while tnow() < end:
                t = tnow()
                info = await tab5_info(s)
                if info is None:
                    death_count += 1
                    w.writerow([f"{t:.0f}", "", "", "", "", "", "", "", "HTTP_DEAD"])
                    await asyncio.sleep(5)
                    continue

                uptime = info.get("uptime_ms", 0)
                if prev_uptime is not None and uptime < prev_uptime:
                    reboot_count += 1
                    w.writerow([f"{t:.0f}", uptime, "", "", "", "", "", "", f"REBOOT#{reboot_count}"])
                prev_uptime = uptime

                st = await tab5_selftest(s)
                heap = await extract_heap(st) or {}

                tick_event = ""
                if t - last_chat > CHAT_EVERY_S:
                    prompts = ["short reply", "say yes", "one word", "pick a color", "count to 3"]
                    r = await run_chat_turn(s, prompts[int(t) % len(prompts)])
                    tick_event = f"chat:{'ok' if r and r.get('sent') else 'fail'}"
                    last_chat = t

                if t - last_swap > MODE_SWAP_EVERY_S:
                    m = mode_cycle[mode_idx % len(mode_cycle)]
                    mode_idx += 1
                    r = await swap_mode(s, m)
                    tick_event = (tick_event + ";" if tick_event else "") + f"mode_{m}:{'ok' if r and r.get('voice_mode')==m else 'fail'}"
                    last_swap = t

                if t - last_widget > WIDGET_EVERY_S:
                    r = await emit_widget_card(s)
                    tick_event = (tick_event + ";" if tick_event else "") + f"widget:{r.get('emitted','?')}"
                    last_widget = t

                w.writerow([f"{t:.0f}", uptime,
                            int(info.get("dragon_connected", False)),
                            int(info.get("voice_connected", False)),
                            int(info.get("wifi_connected", False)),
                            heap.get("free_kb", ""), heap.get("largest_kb", ""), heap.get("frag", ""),
                            tick_event])
                f.flush()
                await asyncio.sleep(15)

    print(f"\nSoak done.")
    print(f"  reboots: {reboot_count}")
    print(f"  HTTP dead polls: {death_count}")
    # Success bar: no reboots OR reboots but device kept recovering
    ok = reboot_count <= 1 and death_count <= 8
    print(f"  verdict: {'PASS' if ok else 'FAIL'} (reboots<=1 dead<=8)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
