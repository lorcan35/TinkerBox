#!/usr/bin/env python3
"""LFM2.5-VL driving browser-harness-js on Dragon.

Verb registry (5 verbs total — narrow surface, not raw CDP):
  browser_navigate(url)
  browser_read(selector?)          - selector defaults to body
  browser_click(selector)
  browser_screenshot()
  browser_done(answer)

Loop:
  user_goal → LFM emits a verb → execute via SSH'd browser-harness-js
            → feed observation back as next turn → repeat until done
            (or step_limit reached).

All transport via SSH to Dragon; LFM lives on the same box at :1234.
"""
from __future__ import annotations
import argparse, json, re, subprocess, sys, time

DRAGON = "radxa@192.168.1.91"
SSH_PASS = "thedragon"
STEP_LIMIT = 6

SYSTEM = """You are a browser agent. Available verbs:
  browser_navigate(url)
  browser_read(selector)
  browser_click(selector)
  browser_screenshot()
  browser_done(answer)

Output ONE verb per turn in this format:
  <|tool_call_start|>[verb_name(arg="value")]<|tool_call_end|>

When you have enough info, output browser_done(answer="..."). Never explain. Never repeat a verb you already called."""

RE_LFM = re.compile(
    r"<\|tool_call_start\|>\s*\[?\s*([a-z_]+)\s*\(([^)]*)\)",
    re.I | re.DOTALL,
)
# Follow-up turns often drop the sentinel tokens.  Accept a bare
# `verb_name(args)` IF the name is one of our registered verbs.
VERB_NAMES = {
    "browser_navigate", "browser_read", "browser_click",
    "browser_screenshot", "browser_done",
}
RE_BARE = re.compile(
    r"\b(browser_(?:navigate|read|click|screenshot|done))\s*\(([^)]*)\)",
    re.I | re.DOTALL,
)


def parse_call(text: str) -> tuple[str, dict] | None:
    import ast
    m = RE_LFM.search(text) or RE_BARE.search(text)
    if not m:
        return None
    name = m.group(1).lower()
    if name not in VERB_NAMES:
        return None
    args_src = m.group(2).strip()
    args: dict = {}
    if args_src:
        try:
            tree = ast.parse(f"_({args_src})", mode="eval")
            call = tree.body  # type: ignore[attr-defined]
            for kw in call.keywords:  # type: ignore[attr-defined]
                if kw.arg is None:
                    continue
                try:
                    args[kw.arg] = ast.literal_eval(kw.value)
                except Exception:
                    pass
        except Exception:
            pass
    return name, args


def llm_step(history: list[dict]) -> tuple[str, float]:
    body = json.dumps({
        "model": "default",
        "messages": history,
        "max_tokens": 120,
        "temperature": 0.1,
        "min_p": 0.15,
        "repetition_penalty": 1.05,
    })
    cmd = [
        "sshpass", "-p", SSH_PASS, "ssh", "-o", "StrictHostKeyChecking=no",
        DRAGON,
        f"curl -s -m 120 -X POST http://127.0.0.1:1234/v1/chat/completions "
        f"-H 'Content-Type: application/json' -d {json.dumps(body)!s}",
    ]
    t0 = time.monotonic()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=140)
    dt = time.monotonic() - t0
    try:
        return json.loads(r.stdout)["choices"][0]["message"]["content"], dt
    except Exception as e:
        return f"<<ERROR {e}: {r.stdout[:200]}>>", dt


def harness_eval(js: str) -> tuple[str, int]:
    """Run a JS snippet through browser-harness-js on Dragon. Returns (stdout, returncode)."""
    cmd = [
        "sshpass", "-p", SSH_PASS, "ssh", "-o", "StrictHostKeyChecking=no",
        DRAGON,
        f"PATH=$HOME/.bun/bin:$HOME/.local/bin:$PATH browser-harness-js {json.dumps(js)}",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return (r.stdout.rstrip() if r.returncode == 0 else r.stderr.rstrip()), r.returncode


def ensure_attached() -> None:
    """Connect harness to chromium if not already."""
    # The harness CLI server is persistent; once connected, subsequent
    # calls reuse the session. But after a chromium restart we MUST
    # re-connect with a fresh wsUrl.
    cmd = [
        "sshpass", "-p", SSH_PASS, "ssh", DRAGON,
        "WS=$(curl -s http://127.0.0.1:9222/json/version "
        "| python3 -c 'import sys,json; print(json.load(sys.stdin)[\"webSocketDebuggerUrl\"])') "
        "&& PATH=$HOME/.bun/bin:$HOME/.local/bin:$PATH "
        "browser-harness-js \"await session.connect({wsUrl: '$WS'}); "
        "const tabs=await listPageTargets(); "
        "if(tabs.length>0){await session.use(tabs[0].targetId);} "
        "await session.Page.enable(); return 'attached';\"",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    print(f"[setup] {r.stdout.strip()}", file=sys.stderr)


def execute_verb(name: str, args: dict) -> str:
    """Map a verb to a CDP-driving JS snippet, return observation text."""
    if name == "browser_navigate":
        url = args.get("url", "").strip()
        if not url:
            return "ERROR: browser_navigate requires url arg"
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        js = (
            f"await session.Page.navigate({{url: {json.dumps(url)}}}); "
            "await new Promise(r => setTimeout(r, 2000)); "
            "const {result} = await session.Runtime.evaluate("
            "{expression: 'document.title', returnByValue: true}); "
            "return 'navigated to ' + " + json.dumps(url) + " + '; title: ' + result.value;"
        )
        out, rc = harness_eval(js)
        return out if rc == 0 else f"ERROR: {out}"

    if name == "browser_read":
        sel = args.get("selector") or "body"
        js = (
            "const {result} = await session.Runtime.evaluate({"
            f"expression: 'document.querySelector({json.dumps(sel)})?.innerText?.slice(0, 800) ?? \"<not found>\"', "
            "returnByValue: true}); return result.value;"
        )
        out, rc = harness_eval(js)
        return f"text from {sel!r}: {out}" if rc == 0 else f"ERROR: {out}"

    if name == "browser_click":
        sel = args.get("selector", "")
        if not sel:
            return "ERROR: browser_click requires selector arg"
        js = (
            "const {result} = await session.Runtime.evaluate({"
            f"expression: '(()=>{{const el=document.querySelector({json.dumps(sel)}); "
            "if(!el)return\"not found\"; el.click(); return\"clicked\";}})()', "
            "returnByValue: true}); return result.value;"
        )
        out, rc = harness_eval(js)
        # Settle for JS-triggered nav
        time.sleep(2)
        return f"click {sel!r}: {out}" if rc == 0 else f"ERROR: {out}"

    if name == "browser_screenshot":
        js = (
            "const shot = await session.Page.captureScreenshot({format: 'png'}); "
            "const buf = Buffer.from(shot.data, 'base64'); "
            "await Bun.write('/tmp/agent_screenshot.png', buf); "
            "return 'screenshot saved (' + buf.length + ' bytes)';"
        )
        out, rc = harness_eval(js)
        return out if rc == 0 else f"ERROR: {out}"

    if name == "browser_done":
        return f"__DONE__::{args.get('answer', '(no answer provided)')}"

    return f"ERROR: unknown verb {name!r}"


def run_goal(goal: str) -> None:
    print(f"=== USER GOAL: {goal} ===\n")
    history = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": goal},
    ]
    used_verbs: list[str] = []
    for step in range(1, STEP_LIMIT + 1):
        used_verbs_str = "; ".join(used_verbs) if used_verbs else "(none yet)"
        text, dt = llm_step(history)
        call = parse_call(text)
        if not call:
            print(f"[step {step}] ({dt:.1f}s) NO VERB EMITTED")
            print(f"  llm: {text.strip()[:200]}")
            return
        name, args = call
        used_verbs.append(f"{name}({json.dumps(args)})")
        print(f"[step {step}] ({dt:.1f}s) → {name}({args})")
        if name == "browser_done":
            print(f"\n=== ANSWER: {args.get('answer', '?')} ===")
            return
        obs = execute_verb(name, args)
        if obs.startswith("__DONE__::"):
            print(f"\n=== ANSWER: {obs[len('__DONE__::'):]} ===")
            return
        print(f"  obs: {obs[:300]}")
        history.append({"role": "assistant", "content": text})
        # Track which verbs+args have already been called and inject
        # a no-repeat hint, since LFM-Q4 oscillates on the same call.
        history.append({"role": "user", "content": (
            f"Result: {obs[:600]}\n\n"
            f"GOAL: {goal}\n"
            f"VERBS USED SO FAR: {used_verbs_str}\n\n"
            "If you have the answer, output ONLY: "
            "<|tool_call_start|>[browser_done(answer=\"<your answer>\")]<|tool_call_end|>\n"
            "Otherwise output ONLY the next verb. Do NOT repeat a verb above."
        )})
    print(f"\n=== STEP LIMIT ({STEP_LIMIT}) HIT — agent did not call browser_done ===")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("goal", help="User goal in natural language")
    p.add_argument("--no-attach", action="store_true")
    a = p.parse_args()
    if not a.no_attach:
        ensure_attached()
    run_goal(a.goal)
