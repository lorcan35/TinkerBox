# Release Notes

> User-facing summary of recent changes.  For exhaustive change
> history, see `git log --oneline` or
> [GitHub Releases](https://github.com/lorcan35/TinkerBox/releases).
>
> Format: most recent first.  Each entry calls out what changed, why
> it matters, and which PRs delivered it.

---

## 2026-04-28 — Onboarding sweep

Big push to make the documentation work for *all three* audiences
the project actually has — non-technical users, tinkerers/builders,
and security/protocol researchers.

**For users (normies):**
- New [`docs/getting-started-user.md`](getting-started-user.md) —
  "I just got a Tab5, now what?"  Power-on to first conversation in
  five minutes.  Plain-English explanation of the four voice modes,
  privacy summary, cost note, troubleshooting.
- [`WELCOME.md`](../WELCOME.md) at the repo root — multi-audience
  landing page that branches by intent.

**For tinkerers:**
- New [`CONTRIBUTING.md`](../CONTRIBUTING.md) — workflow, branch
  naming, commit conventions, CI gates.  Extracted from `CLAUDE.md`
  so contributors don't have to read 850 lines of operational
  runbook.
- New [`docs/dev-setup.md`](dev-setup.md) — get from clone to
  iterate-locally for both Dragon (Python) and Tab5 (ESP-IDF)
  sides.  ESP-IDF v5.5.2 install, USB perms, ngrok, Ollama setup.
- New [`docs/adding-a-tool.md`](adding-a-tool.md) — worked example
  building a `dice_roll` tool from scratch.  Test patterns,
  registration, defensive arg parsing, anti-patterns.

**For hackers:**
- New [`SECURITY.md`](../SECURITY.md) (and counterpart in TinkerTab)
  — honest threat model.  Trust boundaries, auth-token lifecycle
  (`DRAGON_API_TOKEN`, `auth_tok`, `OPENROUTER_API_KEY`), data
  inventory (what gets persisted where, what crosses to OpenRouter),
  network exposure map, firmware integrity status (no secure boot
  or flash encryption currently), known limitations, responsible-
  disclosure path, auditor's checklist.

PRs: TinkerBox onboarding sweep (this release).

---

## 2026-04-28 — Architecture + flow traces

Project went from "great runbook for current contributors" to
"readable to someone walking in cold."

- **[`docs/ARCHITECTURE.md`](ARCHITECTURE.md)** — canonical "what is
  TinkerClaw?" doc with mermaid diagrams (component layout, network
  topology, router decision tree).  Every reader's first stop.
- **`docs/flows/`** — three end-to-end traces with file:line refs
  throughout: [voice turn](flows/voice-turn.md), [vision turn](flows/vision-turn.md),
  [video call](flows/video-call.md).  Mermaid sequence diagrams.
  Latency budgets.  Failure-mode tables.
- **[`GLOSSARY.md`](../GLOSSARY.md)** — ~80 terms across both repos.
- TinkerTab gets its own [HARDWARE.md](https://github.com/lorcan35/TinkerTab/blob/main/docs/HARDWARE.md)
  + [GLOSSARY.md](https://github.com/lorcan35/TinkerTab/blob/main/GLOSSARY.md).

PRs: [#190](https://github.com/lorcan35/TinkerBox/pull/190) (TinkerBox), [#302](https://github.com/lorcan35/TinkerTab/pull/302) (TinkerTab).

---

## 2026-04-28 — Doc cleanup

Archived 7 superseded documents to `docs/historical/`, deleted 3
that had no value (a misplaced Python tree, a stale pytest harness,
a git-stash index), and fixed CLAUDE.md drift on endpoint count
(47 → 53), schema tables (6/9 → 11), and test count (153 → 556).

PRs: [#189](https://github.com/lorcan35/TinkerBox/pull/189) (TinkerBox), [#301](https://github.com/lorcan35/TinkerTab/pull/301) (TinkerTab).

---

## 2026-04-27 — Multi-model router

The single most consequential feature shipped this month.  Dragon
now supports a fleet of LLM backends — pick per-turn based on
modalities + tier policy.

**What you can do now:**
- Route text turns to the cheap `deepseek-v4-flash` ($0.14/M in,
  $0.28/M out) but vision turns to `qwen3.6-flash` ($0.25/$1.50,
  multimodal, 1M context) automatically.
- Mix local + cloud + LAN tiers in one fleet.  e.g., ministral-3:3b
  on Dragon for text + LM Studio on a workstation for vision.
- Cross-modal continuity: send a photo, then ask "what colour was
  the chair?" — the follow-up text turn still sees the photo because
  the multimodal user message is persisted in conversation history.

**Reference:**
- [`docs/router-cookbook.md`](router-cookbook.md) — copy-paste fleet
  recipes (default fleet, sub-$0.50/M cheapskate, top-tier-only,
  DGX LAN tier).
- [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) "Multi-Model Router"
  section.
- [`docs/flows/vision-turn.md`](flows/vision-turn.md) for the
  end-to-end trace.

**Provider catalog (verified live 2026-04-27):**
- Anthropic: Sonnet 4.6, Opus 4.7, Haiku 4.5
- OpenAI: GPT-5.5, GPT-5.5 Pro
- Google: Gemini 3.1 Pro Preview (native video!), Gemini 3 Flash Preview, Gemma 4
- DeepSeek: V4 Pro, V4 Flash, R1
- Qwen: 3.6 family (multimodal, 1M context)
- Moonshot Kimi K2.6, x-AI Grok 4.20, Z-ai GLM 5.x, Xiaomi MiMo

PRs: #184 (capability declarations), #185 (router), #186 (server
integration + cross-modal continuity), #187 (OR registry refresh),
#188 (router docs).

---

## 2026-04-27 — Tab5 e2e harness + debug additions

The firmware now has a Python-driven scenario runner that drives
real user flows via the debug API.  Three canonical stories:
`story_smoke` (~2 min, 14 steps), `story_full` (~24 steps),
`story_stress` (10 min, 77 steps).  Reports + screenshots in
`tests/e2e/runs/<scenario>-<ts>/`.

Bonus debug-server additions: `GET /screen`, `POST /input/text`,
seven new `tab5_debug_obs_event` call sites in `voice.c` /
`debug_server.c` / `ui_camera.c`.

PRs: TinkerTab #294 (debug events + /screen), #295 (harness), #297 (/input/text), #299/#300 (input/text scoping fix), #298 (docs).

---

## 2026-04-27 — Tab5 video recording (#291)

REC button on the camera screen.  Records motion-JPEG to
`/sdcard/VID_NNNN.MJP` at 5 fps, then auto-uploads to Dragon's
`/api/media/upload`.

Why `.MJP` and not `.mjpeg`: FATFS LFN is disabled on Tab5 (saves
RAM), so we're stuck with 8.3 short names.  Players sniff magic
bytes; the extension is cosmetic.

PR: TinkerTab #291.

---

## 2026-04-21 — Wave 14 closed

63-item systematic audit (cross-stack: 6 CRITICAL, 23 HIGH, 22
MEDIUM, 12 LOW).  Major fixes shipped over the prior week:

- LVGL `lv_async_call` was NOT thread-safe (does `lv_malloc`
  + `lv_timer_create` against unprotected TLSF).  PR #257 hand-
  wrapped the empirical site; #259 added `tab5_lv_async_call`
  helper and replaced all 49 sites.  100% uptime in 5-min mixed
  nav+screenshot stress (was 50% pre-fix).
- WS keepalive during inference (PR #76) — local-mode 4B-class
  models routinely take 60-90s per turn; without keepalive pings
  Tab5's PONG-watch evicted in-flight LLM streams.
- Empty-reply guard (PR #77/#79) — FC-trained models that emit a
  tool call and stop now get a one-line natural-language ack
  synthesised from the tool result.
- Default Local LLM swapped to ministral-3:3b after an 11-model
  gauntlet.

Full audit: [`docs/historical/AUDIT-WAVE-14.md`](historical/AUDIT-WAVE-14.md).
Wave 15 active: [`docs/AUDIT-WAVE-15.md`](AUDIT-WAVE-15.md).

---

## Earlier history

For releases before 2026-04, see `git log --oneline` or the closed
issues marked `release/*`.  CHANGELOG.md was archived to
`docs/historical/` as it stopped updating around 2026-04-01 — the
manually-maintained format didn't keep up with the post-router
work.
