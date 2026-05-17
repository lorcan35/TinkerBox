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
