#!/usr/bin/env python3
"""UI-TARS-1.5-7B driving Chromium via browser-harness on Dragon.

UI-TARS is screenshot-conditioned: every turn ships a fresh PNG so the
model sees the current page.  The model emits a `Thought:` + `Action:`
block; we parse the Action, dispatch via the harness, then capture a
new screenshot and feed it back as the next user turn.

Action space (COMPUTER_USE_DOUBAO):
  click(point='<point>X Y</point>')           # X,Y in 0..1000 normalized
  left_double(point='<point>X Y</point>')
  right_single(point='<point>X Y</point>')
  drag(start_point='<point>X Y</point>', end_point='<point>X Y</point>')
  hotkey(key='ctrl c')                        # space-separated, ≤3 keys
  type(content='xxx')                         # \\n at end submits
  scroll(point='<point>X Y</point>', direction='down|up|left|right')
  wait()                                      # sleep 5 s then screenshot
  finished(content='answer')                  # terminate
"""
from __future__ import annotations
import argparse, base64, json, re, subprocess, sys, time

DRAGON = "radxa@192.168.1.91"
SSH_PASS = "thedragon"
STEP_LIMIT = 4

# Filled in at startup from page_info().  UI-TARS emits coords in
# 0..1000 normalised; we scale to actual chromium viewport.
VIEWPORT_W = 780
VIEWPORT_H = 441

SYSTEM_TPL = """You are a GUI agent. You are given a task and your action history, with screenshots. You need to perform the next action to complete the task.

## Output Format
```
Thought: ...
Action: ...
```

## Action Space

click(point='<point>x1 y1</point>')
left_double(point='<point>x1 y1</point>')
right_single(point='<point>x1 y1</point>')
drag(start_point='<point>x1 y1</point>', end_point='<point>x2 y2</point>')
hotkey(key='ctrl c')
type(content='xxx')
scroll(point='<point>x1 y1</point>', direction='down or up or right or left')
wait()
finished(content='xxx')


## Note
- Use English in `Thought` part.
- Write a small plan and finally summarize your next action (with its target element) in one sentence in `Thought` part.

## User Instruction
{instruction}
"""

ACTION_RE = re.compile(r"Action:\s*(.+?)(?:\n|$)", re.DOTALL)
POINT_RE = re.compile(r"<point>\s*(\d+)\s+(\d+)\s*</point>")


def ssh_run(remote_cmd: str, timeout: int = 60) -> tuple[str, int]:
    cmd = [
        "sshpass", "-p", SSH_PASS, "ssh",
        "-o", "StrictHostKeyChecking=no",
        DRAGON, remote_cmd,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return (r.stdout.rstrip() if r.returncode == 0 else r.stderr.rstrip()), r.returncode


def harness_exec(py_src: str) -> tuple[str, int]:
    """Execute Python through the Python browser-harness on Dragon."""
    remote = (
        "export PATH=$HOME/.local/bin:$PATH && "
        "BU_CDP_URL=http://127.0.0.1:9222 browser-harness"
    )
    cmd = [
        "sshpass", "-p", SSH_PASS, "ssh", "-o", "StrictHostKeyChecking=no",
        DRAGON, f"{remote} <<'PYEOF'\n{py_src}\nPYEOF",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    return (r.stdout.rstrip() if r.returncode == 0 else r.stderr.rstrip()), r.returncode


def screenshot_b64() -> str:
    """Capture current chromium screenshot, return base64 PNG."""
    py = "p = capture_screenshot('/tmp/uitars_shot.png'); print(p)"
    out, rc = harness_exec(py)
    if rc != 0:
        return ""
    # Fetch the file via base64 over SSH
    cmd = [
        "sshpass", "-p", SSH_PASS, "ssh", "-o", "StrictHostKeyChecking=no",
        DRAGON, "base64 -w0 /tmp/uitars_shot.png",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    return r.stdout.strip() if r.returncode == 0 else ""


def scale(x: int, y: int) -> tuple[int, int]:
    """UI-TARS emits 0..1000 normalized coords; scale to viewport pixels."""
    return (int(x * VIEWPORT_W / 1000), int(y * VIEWPORT_H / 1000))


def execute_action(action: str) -> tuple[bool, str]:
    """Dispatch a UI-TARS Action call to the harness. Returns (terminate, observation)."""
    a = action.strip()
    if a.startswith("finished("):
        m = re.search(r"content=['\"](.+?)['\"]\s*\)\s*$", a, re.DOTALL)
        ans = m.group(1) if m else "(no content arg)"
        return True, f"FINISHED: {ans}"

    if a.startswith("click(") or a.startswith("left_double(") or a.startswith("right_single("):
        m = POINT_RE.search(a)
        if not m:
            return False, "ERROR: couldn't parse <point>"
        x, y = scale(int(m.group(1)), int(m.group(2)))
        button = "left"
        clicks = 1
        if a.startswith("left_double("):
            clicks = 2
        elif a.startswith("right_single("):
            button = "right"
        py = f"click_at_xy({x}, {y}, button={button!r}, clicks={clicks}); print('clicked', {x}, {y})"
        out, rc = harness_exec(py)
        time.sleep(2)
        return False, out if rc == 0 else f"ERROR: {out}"

    if a.startswith("type("):
        m = re.search(r"content=['\"](.*?)['\"]\s*\)\s*$", a, re.DOTALL)
        if not m:
            return False, "ERROR: couldn't parse type content"
        text = m.group(1).encode().decode("unicode_escape")
        py = f"type_text({text!r}); print('typed', len({text!r}), 'chars')"
        out, rc = harness_exec(py)
        return False, out if rc == 0 else f"ERROR: {out}"

    if a.startswith("hotkey("):
        m = re.search(r"key=['\"](.+?)['\"]", a)
        if not m:
            return False, "ERROR: couldn't parse hotkey"
        keys = m.group(1).split()
        # Map first key only for simplicity (most navigation uses single)
        py = f"press_key({keys[-1]!r}); print('pressed', {keys!r})"
        out, rc = harness_exec(py)
        return False, out if rc == 0 else f"ERROR: {out}"

    if a.startswith("scroll("):
        m = POINT_RE.search(a)
        if m:
            x, y = scale(int(m.group(1)), int(m.group(2)))
        else:
            x, y = VIEWPORT_W // 2, VIEWPORT_H // 2
        d = re.search(r"direction=['\"](\w+)['\"]", a)
        direction = d.group(1) if d else "down"
        dy = -300 if direction == "down" else (300 if direction == "up" else 0)
        dx = -300 if direction == "right" else (300 if direction == "left" else 0)
        py = f"scroll({x}, {y}, dy={dy}, dx={dx}); print('scrolled', {direction!r})"
        out, rc = harness_exec(py)
        time.sleep(1)
        return False, out if rc == 0 else f"ERROR: {out}"

    if a.startswith("wait()"):
        time.sleep(5)
        return False, "waited 5s"

    return False, f"ERROR: unrecognized action {a[:80]!r}"


def llm_step(history: list[dict]) -> tuple[str, float]:
    body = json.dumps({
        "model": "default",
        "messages": history,
        "max_tokens": 160,
        "temperature": 0.0,
    })
    # Send body via stdin to avoid SSH arg-length limits with base64 images
    remote_cmd = (
        "curl -s -m 600 -X POST http://127.0.0.1:1234/v1/chat/completions "
        "-H 'Content-Type: application/json' --data-binary @-"
    )
    cmd = [
        "sshpass", "-p", SSH_PASS, "ssh", "-o", "StrictHostKeyChecking=no",
        DRAGON, remote_cmd,
    ]
    t0 = time.monotonic()
    r = subprocess.run(cmd, input=body, capture_output=True, text=True, timeout=720)
    dt = time.monotonic() - t0
    try:
        return json.loads(r.stdout)["choices"][0]["message"]["content"], dt
    except Exception as e:
        return f"<<ERROR {e}: {r.stdout[:200]}>>", dt


def connect_harness() -> None:
    """Ensure harness is attached to chromium."""
    ssh_run(
        "export PATH=$HOME/.local/bin:$PATH && "
        "WS=$(curl -s http://127.0.0.1:9222/json/version "
        "| python3 -c 'import sys,json; print(json.load(sys.stdin)[\"webSocketDebuggerUrl\"])') "
        "&& BU_CDP_URL=http://127.0.0.1:9222 browser-harness "
        "\"await session.connect({wsUrl: '$WS'}); "
        "const tabs=await listPageTargets(); "
        "if(tabs.length>0){await session.use(tabs[0].targetId);} "
        "await session.Page.enable(); return 'attached';\""
    )


def run_goal(goal: str, start_url: str | None = None) -> None:
    global VIEWPORT_W, VIEWPORT_H
    print(f"=== USER GOAL: {goal} ===")
    if start_url:
        print(f"=== START URL: {start_url} ===")
    print()
    connect_harness()
    # Pre-navigate if a start URL is provided.  UI-TARS's action space
    # has no `navigate()` verb — the model was trained to operate on
    # already-loaded pages.  We handle the URL outside the agent loop.
    if start_url:
        py = (
            f"goto_url({start_url!r}); "
            "wait_for_load(timeout=10); "
            "print(page_info())"
        )
        out, rc = harness_exec(py)
        print(f"pre-nav: {out[:200]}")
    # Pull actual viewport so action coord-scaling matches reality
    info_out, _ = harness_exec("import json; print(json.dumps(page_info()))")
    try:
        info = json.loads(info_out)
        VIEWPORT_W = info.get("w", VIEWPORT_W)
        VIEWPORT_H = info.get("h", VIEWPORT_H)
        print(f"viewport: {VIEWPORT_W}x{VIEWPORT_H}")
    except Exception:
        print(f"viewport probe failed, using {VIEWPORT_W}x{VIEWPORT_H}")
    system = SYSTEM_TPL.format(instruction=goal)
    history: list[dict] = [{"role": "system", "content": system}]
    for step in range(1, STEP_LIMIT + 1):
        # Capture fresh screenshot
        shot = screenshot_b64()
        if not shot:
            print(f"[step {step}] screenshot failed")
            return
        user_content = [
            {"type": "text", "text": f"(step {step})"},
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{shot}"}},
        ]
        history.append({"role": "user", "content": user_content})
        text, dt = llm_step(history)
        print(f"[step {step}] ({dt:.1f}s) llm output ({len(text)} chars)")
        # Strip thought line for terse logs
        lines = [ln for ln in text.splitlines() if ln.strip()]
        for ln in lines[:3]:
            print(f"   | {ln[:140]}")
        # Find the Action line
        m = ACTION_RE.search(text)
        if not m:
            print(f"   ! no 'Action:' line found, stopping")
            return
        action = m.group(1).strip().split("\n")[0]
        print(f"   => {action[:140]}")
        history.append({"role": "assistant", "content": text})
        terminate, obs = execute_action(action)
        print(f"   obs: {obs[:200]}")
        if terminate:
            print(f"\n=== ANSWER: {obs[len('FINISHED: '):]} ===")
            return
    print(f"\n=== STEP LIMIT ({STEP_LIMIT}) hit ===")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("goal")
    p.add_argument("--url", help="Pre-navigate before invoking the agent")
    a = p.parse_args()
    run_goal(a.goal, start_url=a.url)
