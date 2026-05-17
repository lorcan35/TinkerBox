#!/usr/bin/env python3
"""Same verb registry as agent_lfm.py, but executes via the Python
browser-harness on Dragon (high-level helpers: goto_url, page_info,
js, capture_screenshot, etc.) instead of browser-harness-js raw CDP.
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
    })
    cmd = [
        "sshpass", "-p", SSH_PASS, "ssh", "-o", "StrictHostKeyChecking=no",
        DRAGON,
        f"curl -s -m 180 -X POST http://127.0.0.1:1234/v1/chat/completions "
        f"-H 'Content-Type: application/json' -d {json.dumps(body)!s}",
    ]
    t0 = time.monotonic()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=200)
    dt = time.monotonic() - t0
    try:
        return json.loads(r.stdout)["choices"][0]["message"]["content"], dt
    except Exception as e:
        return f"<<ERROR {e}: {r.stdout[:200]}>>", dt


def harness_exec(py_src: str) -> tuple[str, int]:
    """Execute a Python snippet through browser-harness on Dragon."""
    remote = (
        "export PATH=$HOME/.local/bin:$PATH && "
        "BU_CDP_URL=http://127.0.0.1:9222 "
        "browser-harness"
    )
    cmd = [
        "sshpass", "-p", SSH_PASS, "ssh", "-o", "StrictHostKeyChecking=no",
        DRAGON,
        f"{remote} <<'PYEOF'\n{py_src}\nPYEOF",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    out = r.stdout.rstrip() if r.returncode == 0 else r.stderr.rstrip()
    return out, r.returncode


def execute_verb(name: str, args: dict) -> str:
    if name == "browser_navigate":
        url = (args.get("url") or "").strip()
        if not url:
            return "ERROR: browser_navigate requires url arg"
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        py = f"goto_url({url!r}); wait_for_load(timeout=8); print(page_info())"
        out, rc = harness_exec(py)
        return f"navigated; {out}" if rc == 0 else f"ERROR: {out}"

    if name == "browser_read":
        sel = args.get("selector") or "body"
        js_expr = (
            f"(document.querySelector({json.dumps(sel)})?.innerText ?? "
            "'<not found>').slice(0, 800)"
        )
        py = f"print(js({json.dumps(js_expr)}))"
        out, rc = harness_exec(py)
        return f"text from {sel!r}: {out}" if rc == 0 else f"ERROR: {out}"

    if name == "browser_click":
        sel = args.get("selector", "")
        if not sel:
            return "ERROR: browser_click requires selector arg"
        js_expr = (
            "(()=>{const el=document.querySelector("
            f"{json.dumps(sel)}); if(!el)return'not found'; "
            "el.click(); return'clicked';})()"
        )
        py = f"print(js({json.dumps(js_expr)}))"
        out, rc = harness_exec(py)
        time.sleep(2)
        return f"click {sel!r}: {out}" if rc == 0 else f"ERROR: {out}"

    if name == "browser_screenshot":
        py = "p = capture_screenshot('/tmp/agent_py.png'); print('saved', p)"
        out, rc = harness_exec(py)
        return out if rc == 0 else f"ERROR: {out}"

    if name == "browser_done":
        return f"__DONE__::{args.get('answer', '(no answer)')}"

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
    p.add_argument("goal")
    a = p.parse_args()
    run_goal(a.goal)
