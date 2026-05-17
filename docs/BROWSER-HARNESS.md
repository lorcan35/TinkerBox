# browser-harness-js on Dragon (2026-05-17)

[browser-use/browser-harness-js](https://github.com/browser-use/browser-harness-js)
installed on Dragon Q6A.  It's a Bun HTTP server that exposes Chrome
DevTools Protocol — 56 domains, 652 typed methods — as JavaScript
callable through a CLI.  No LLM loop is wired yet; the harness is
parked ready for an agent to drive it.

## Why install with no agent

The natural pairing is LFM2.5-VL (current Local default), but LFM-VL
scored 7/20 on the messy-prose tool-routing gauntlet
(see `CLAUDE.md` "LFM2.5-VL-1.6B HARD gauntlet").  Browser automation
is harder than the gauntlet, not easier, so the wiring is deferred
until either:

- A model lands that scores ≥80% on a browser-specific eval, OR
- We accept LLM-flakiness behind a human-in-the-loop confirm step.

For now the harness lives on Dragon so we can hand-drive it for
demos, manual scraping, or to test future agent loops in isolation.

## Install state on Dragon

| Component | Path | Notes |
|---|---|---|
| Bun runtime | `/home/radxa/.bun/bin/bun` | v1.3.14, installed via `curl -fsSL https://bun.sh/install | bash` |
| Repo clone | `/home/radxa/browser-harness-js/` | `git clone` from upstream |
| CLI | `/home/radxa/.local/bin/browser-harness-js` | symlinked from `sdk/browser-harness-js` |
| Chrome binary | `/snap/chromium/current/usr/lib/chromium-browser/chrome` | 146.0.7680.164 ARM64, invoked DIRECTLY (snapd is masked on this box) |
| systemd unit | `dragon-chromium.service` | keeps headless Chrome on `:9222`, profile in `~/chrome-profile/` |
| Demos | `/home/radxa/browser-harness-js/DEMOS.md` | five verified CDP snippets — list tabs, navigate, scrape DOM, screenshot, count links |

The browser-harness-js Bun server (port `:9876`) **auto-spawns** on
first CLI invocation — no systemd unit needed for it.

## Auto-detect does NOT work on Dragon

`session.connect()` with no args scans well-known profile dirs and
ours (`~/chrome-profile/`) is non-standard.  Always pass an explicit
`wsUrl` after fetching it from `:9222/json/version`:

```bash
WS=$(curl -s http://127.0.0.1:9222/json/version \
       | python3 -c "import sys,json; print(json.load(sys.stdin)['webSocketDebuggerUrl'])")
browser-harness-js "await session.connect({wsUrl: \"$WS\"}); return \"connected\""
```

After a `sudo systemctl restart dragon-chromium` the WS endpoint UUID
changes, so you MUST re-connect.

## Verified hand-driven demos

All run live 2026-05-17.  See `DEMOS.md` on Dragon for the exact JS.

1. **List tabs** — `listPageTargets()` returns the one default about:blank tab.
2. **Navigate + read title** — `session.Page.navigate({url})` + a
   `Runtime.evaluate({expression: "document.title"})` returned
   `"Example Domain"`.
3. **Multi-selector scrape** — pulled `h1` + first `p` from example.com.
4. **Screenshot to PNG** — `Page.captureScreenshot` returned 16624 bytes
   at 780×441; the PNG round-tripped back to the workstation cleanly.
5. **Navigate Google + count links** — 20 anchor tags on the home page.

## First agent-loop attempt (2026-05-17, LFM2.5-VL Q4_0)

A minimal 5-verb agent (`experiments/agent_lfm_browser.py`) was
tried against the user story:

> "Go to example.com and tell me what the page says"

**What worked:**
- Connecting to chromium via the harness (`session.connect({wsUrl})`)
- Step 1: LFM emitted `browser_navigate(url="example.com")`; harness
  navigated successfully, returned the title
- Step 2: LFM emitted `browser_read(selector="body")`; harness
  returned the full innerText of the page
- After step 2 the model HAS the answer — "Example Domain. This
  domain is for use in documentation examples without needing
  permission. Avoid use in operations."

**What broke (across four prompt iterations):**

1. Model never reliably emits `browser_done(answer="...")` even
   when the observation contains the answer.  It keeps probing
   selectors (`nav`, `main`, `footer`, `header` — all "<not found>")
   or oscillates back to `browser_navigate`.
2. On follow-up turns it occasionally drops the
   `<|tool_call_start|>...<|tool_call_end|>` sentinels and emits
   bare `verb(args)` — workable with a more lenient parser but a
   sign that multi-turn instruction-following degrades.
3. Drops the `https://` prefix on URLs after the first turn —
   workable with a defensive normalizer.
4. Eventually emits `browser_navigate()` with no `url=` arg —
   the same arg-extraction failure mode the hard gauntlet exposed.
5. After seeing an error observation, it does NOT change strategy —
   keeps re-emitting the same broken call.

**Prompt variants tried:**
- Long system prompt with explicit stopping rule
- Long system prompt + few-shot example showing nav→read→done
- Minimal system prompt + per-turn "no-repeat" reminder listing
  every verb already called
- Defensive URL normalization in the harness layer

None of these closed the loop.

**Diagnosis:** LFM-VL Q4_0 at 1.6 B (effective ~1.2 B) is below the
threshold for reliable multi-turn state tracking.  This matches the
hard-gauntlet 7/20 finding — arg extraction and state tracking are
the model's weakest axes, and a browser agent needs both.

**Q8_0 bump (2026-05-17, same session):** pulled the 1.25 GB Q8_0
quant, restarted llama-server, re-ran the example.com story.  Result:
Q8 is cleaner per-call (no dropped `https://`, picks `selector="title"`
first which is smarter than blindly reading `body`) BUT exhibits the
SAME structural failure — never calls `browser_done`, devolves to
empty `browser_navigate()` after step 3.  Same trace with temp=0 +
repetition_penalty=1.3.  The gap is the model's parameter count
(1.6 B), not its precision.

**Path forward (still deferred — none of these is a 30-min wire-up):**
- Try LFM Q8_0 (1.25 GB vs 696 MB) — quality lift may close the gap
- Try LFM2-2.6B if/when a GGUF lands — 60% more effective params
- Add a hand-coded "I have an answer" detector that auto-fires
  browser_done when an observation looks complete (heuristic, not LLM)
- Switch to a model with proper agentic tool-use training
  (Hermes-3, Mistral-Tool, Qwen-Coder) when we find one that runs
  at acceptable speed on Q6A
- Move the agent loop OFF Dragon — run it from the workstation
  driving the Dragon harness, with a more capable orchestrator LLM

## When wiring up an LLM loop (deferred)

Don't expose the raw CDP surface (652 methods) — wrap a small verb
registry the LLM can route to.  Suggested verbs:

| Verb | Maps to |
|---|---|
| `browser_navigate(url)` | `Page.navigate` + 1.5 s settle |
| `browser_click(selector)` | `Runtime.evaluate(document.querySelector(s).click())` |
| `browser_type(selector, text)` | `Input.dispatchKeyEvent` chain |
| `browser_read(selector)` | `Runtime.evaluate(...innerText)` |
| `browser_screenshot()` | `Page.captureScreenshot` → base64 image_url back into the LLM (LFM-VL can consume it directly) |

Use Dragon's existing system prompt + Dialect 5 + the "tool-router,
never refuse" directive that got LFM-VL to 10/10 on the easy gauntlet.
Loop: user-goal → router → verb → harness → result → next verb.

Note: vision-mode LFM-VL can take a screenshot AS ITS OWN INPUT —
which gives us an interesting loop: model takes screenshot → routes
next click based on what it sees → etc.  ~40 s per vision turn on Q6A
(prompt-process dominates) is the latency budget.
