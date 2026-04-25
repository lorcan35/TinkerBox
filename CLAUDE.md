# TinkerBox — Dragon Server Stack (THE BRAIN)

## Active investigations — READ FIRST before related work

- **UX-gap remediation (2026-04-25)** → see [`docs/UX-GAPS.md`](docs/UX-GAPS.md)
  + master issue [#89](https://github.com/lorcan35/TinkerBox/issues/89).
  21 verified gaps across 6 phases.  Phase 1 = WS dispatcher discipline
  (text/multimodal cancel works, mode-swap stops freezing).  Read the doc
  before touching anything in `_handle_text`, `_handle_user_media`,
  `_handle_config_update`, `pipeline._process_utterance`, error emission
  sites, dictation post-process, or the WS dispatcher.

## Repo Separation — READ THIS FIRST
- **TinkerBox** (this repo) = Dragon Q6A server. Python. ALL intelligence lives here.
  - Owns: STT, LLM, TTS, embeddings, session management, conversation engine, REST API, dashboard, database
  - Tab5 is a THIN CLIENT. Dragon is the BRAIN.
- **TinkerTab** (github.com/lorcan35/TinkerTab) = Tab5 firmware. C/ESP-IDF. Display + sensors only.
  - Owns: LVGL UI, mic/speaker, camera, touch, SD card, WiFi, NVS settings
  - Sends audio/text to Dragon, receives responses. No AI logic on Tab5.
- **Protocol:** `docs/protocol.md` defines the WebSocket contract between them. Both repos reference it.

## Overview
TinkerBox runs on a Dragon Q6A (Radxa, Qualcomm QCS6490) and provides:
- Session management + conversation engine (port 3502)
- Voice pipeline: STT → LLM → TTS (port 3502)
- REST API for sessions, notes, devices, config (port 3502)
- CDP browser streaming to Tab5 (port 3501)
- Web dashboard for device management (port 3500)
- mDNS service discovery

Companion repo: [TinkerTab](https://github.com/lorcan35/TinkerTab) (ESP32-P4 Tab5 firmware)

## MANDATORY: Check LEARNINGS.md First
Before writing any fix, CHECK LEARNINGS.md first. Your bug might already be documented. Every bug found, every fix, every gotcha MUST be added to LEARNINGS.md with Date/Symptom/Root Cause/Fix/Prevention.

## Workflow

### Basic flow
1. **Issue first** — Create a GitHub issue before starting work (`gh issue create`).  Cross-stack audit items already have Wave IDs (`W14-C01`, `W15-H09`, …) — reuse them instead of opening a duplicate.
2. **Branch** — `feat/<slug>`, `fix/<slug>`, `chore/<slug>`, `docs/<slug>`, `investigate/<slug>`.  Branch from main.
3. **Commit** — Conventional-commit prefix (`feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`) + one-line subject + `closes #N` or `refs #N`.  One logical change per commit.  One feature or refactor per PR.
4. **Push, open PR** — Let CI run (see "CI gates" below).  Squash-merge.  Delete the branch.  Don't force-push shared branches.

### PR scope discipline (the one that saves review hours)
- **One concern per PR.**  Refactors don't contain bug fixes.  Bug fixes don't contain "while we're here" cleanups.  Doc updates don't contain behavior changes.  If you find something else that needs fixing mid-PR, open a new issue and move on.
- **Extract before decompose.**  When splitting a large file, the first PR *moves* code to its new home with identical behavior — no restructuring of the internals.  Decomposing the internals is follow-up PRs.  This keeps each diff reviewable in minutes rather than hours.
- **Tests move with code.**  If the thing you're extracting has tests, move them in the same commit.  If it has no tests, add at least one before the extraction lands — otherwise you're trading "untested big file" for "untested small files" and the net test coverage drops.
- **Small is kind.**  Prefer 5 small PRs over 1 big one.  Reviewers are more generous to a 200-line diff than a 2,000-line diff, and bisecting a regression across 5 commits beats bisecting across 1.

### CI gates (enforced by `.github/workflows/ci.yml`)
- **Ruff** with a narrow gate: `F821,F722,F811,F823,B006,B904,E722,B007,RUF006`.  This is the real-bug gate — not a style gate.  Adding codes is a two-line change to `ci.yml`; prove a code catches a real bug before promoting it.
- **Named unit tests only.**  E2E (`test_api_e2e.py`, `test_e2e_dragon.py`) run locally, not in CI.  When you add a new test file that can run without a live server, add it to the CI test list in `ci.yml`.
- **CI uses `DRAGON_API_TOKEN=ci-bearer-token` + `TINKERCLAW_TOKEN=ci-tc-token`.**  Tests that need tokens must read them from env, not hardcoded.

### Local pre-push (takes ~10 s)
```bash
ruff check --select F821,F722,F811,F823,B006,B904,E722,B007,RUF006 dragon_voice/ dashboard.py tests/
pytest -q tests/test_auth_middleware.py tests/test_media_pipeline.py tests/test_session_cas.py  # or whatever your PR touches
```
If you touched middleware, run `test_auth_middleware` + `test_security_headers` + `test_rate_limit`.  If you touched media, run `test_media_*`.  Don't run the E2E suite unless you've booted a local server.

### Anti-slop rules (applies to human and AI contributors equally)
- **No defensive code for impossible scenarios.**  Trust internal callers.  Validate at system boundaries (HTTP request, WS frame, NVS read) — not between two functions in the same module.
- **Delete, don't comment out.**  Git remembers.  `# TODO: remove this` is a lie; either fix it in this PR or open an issue.
- **No comments that restate well-named code.**  Comments exist to explain *why*, or to warn about non-obvious constraints.  `# increment counter` above `counter += 1` is noise.
- **No speculative abstractions.**  A factory class with one caller is a one-caller class pretending to be a factory.  Build it when the second caller arrives, not before.
- **No "helpful" refactors next to the feature.**  If it's worth doing, it's worth a separate PR.  If it isn't, drop it.
- **Name things for the reader, not the writer.**  Method names describe what the caller gets; variable names describe what the thing *is*.  `_handle_ws_voice` is a better name than `_process_incoming_voice_socket_request_with_fallback`.
- **LEARNINGS.md is not optional.**  Every bug fix with a non-obvious root cause adds an entry with Date / Symptom / Root Cause / Fix / Prevention.  Skip it only if the fix is genuinely one-line and self-explaining.

### File-split smell test (for refactoring PRs like the `server.py` decomposition)
A file is too big when it has more than one *reason to change*.  Before extracting, answer: "what stakeholder cares about the code I'm moving?"  If it's the same stakeholder as the rest of the file, don't extract yet.  Good candidates: middleware (ops/security), debug endpoints (dev/diagnostics), lifecycle (ops/reliability), business endpoints (product).

## Dragon Access
- **Host:** 192.168.1.91 (static IP on LAN)
- **User:** radxa
- **Password:** `<DRAGON_SSH_PASSWORD>`
- **SSH:** `ssh radxa@192.168.1.91  # password in ~/.ssh/config or use key auth`
- **OS:** Ubuntu (Dragon Q6A — Qualcomm QCS6490, ARM64)
- **Connection:** Ethernet only (WiFi disabled). Static IP on enp1s0.
- **Services stripped:** gdm3, snapd, ollama, nanobot masked. Only tinkerclaw-voice, tinkerclaw-dashboard, tinkerclaw-ngrok run.

## Service Map
| Service | Port | SystemD Unit | Description |
|---------|------|-------------|-------------|
| Dashboard | 3500 | tinkerclaw-dashboard | Web UI for device management |
| Dragon CDP | 3501 | tinkerclaw | CDP browser streaming + touch relay |
| Voice | 3502 | tinkerclaw-voice | STT/LLM/TTS voice pipeline + Notes API routes |
| mDNS | — | tinkerclaw-mdns | Advertises _tinkerclaw._tcp |
| Chromium | 9222 | (launched by tinkerclaw) | CDP target browser |
| SearXNG | 8888 | searxng | Self-hosted metasearch engine (web_search tool backend) |
| Ollama | 11434 | ollama | Local LLM inference (CPU, slow) |
| NPU Genie | — | (via voice pipeline) | Llama 3.2 1B on QCS6490 HTP (~8 tok/s) |
| TinkerClaw GW | 18789 | tinkerclaw-gateway | TinkerClaw sidecar agent runner (localhost only) |
| ngrok | 443 (ext) | tinkerclaw-ngrok | tinkerclaw-dashboard.ngrok.dev → 3500, tinkerclaw-voice.ngrok.dev → 3502, tinkerclaw-gateway.ngrok.dev → 18789 |

## Deploy
```bash
# Sync code to Dragon (includes new STT/TTS backends + notes module)
scp -r dragon_voice/ radxa@192.168.1.91:/home/radxa/
scp dashboard.py radxa@192.168.1.91:/home/radxa/
scp schema.sql radxa@192.168.1.91:/home/radxa/

# Restart services
ssh radxa@192.168.1.91 "sudo systemctl restart tinkerclaw-voice"
```

### Post-Deploy Checklist
- **Clear `__pycache__`:** After `scp` deploy, stale `.pyc` files can cause import errors. Run `find /home/radxa/dragon_voice -name '__pycache__' -exec rm -rf {} +` on Dragon before restarting.
- **API key in .env survives deploys:** The OpenRouter API key is stored in `/home/radxa/.env` (loaded by systemd `EnvironmentFile=`). This file is NOT overwritten by `scp -r dragon_voice/` deploys, so secrets survive code pushes. Do NOT put real API keys in `config.yaml` in the repo.
- **Restore `tinkerclaw_token` in `config.yaml`:** Must match `~/.tinkerclaw/tinkerclaw.json` gateway auth token.
- **ngrok domains:** Three tunnels are active:
  - `tinkerclaw-dashboard.ngrok.dev` → 3500 (dashboard)
  - `tinkerclaw-voice.ngrok.dev` → 3502 (voice)
  - `tinkerclaw-gateway.ngrok.dev` → 18789 (TinkerClaw)

## Three-Tier Voice Mode
Tab5 sends `{"type":"config_update","voice_mode":0|1|2|3,"llm_model":"..."}`. Dragon hot-swaps backends:

| Mode | voice_mode | STT | LLM | TTS |
|------|-----------|-----|-----|-----|
| **Local** | 0 | Moonshine | Local (npu_genie/ollama) | Piper (22050Hz) |
| **Hybrid** | 1 | OpenRouter gpt-audio-mini | Local (unchanged) | OpenRouter gpt-audio-mini (24kHz) |
| **Full Cloud** | 2 | OpenRouter gpt-audio-mini | OpenRouter (user-selected model) | OpenRouter gpt-audio-mini (24kHz) |
| **TinkerClaw** | 3 | Moonshine (or OpenRouter) | TinkerClaw Gateway (agent runner) | Piper (or OpenRouter) |

- **LLM Model Selection:** `llm_model` field selects cloud model: `anthropic/claude-3-haiku`, `anthropic/claude-sonnet-4-20250514`, `openai/gpt-4o-mini`. Stored in `LLMConfig.openrouter_model`.
- **API Key:** Auto-propagated from `llm.openrouter_api_key` to `stt.openrouter_api_key` and `tts.openrouter_api_key`. Validated before swap — rejects with error if empty.
- **Auto-Fallback:** If cloud STT/TTS fails (timeout, API error), pipeline auto-falls back to local (Moonshine/Piper) for that request AND sends `config_update` with `error` field to Tab5 → auto-reverts to Local mode.
- **Backward compat:** Old `cloud_mode` boolean still accepted (maps to voice_mode 0 or 2).
- **Config fields:** `LLMConfig.local_backend` (remembers original for fallback), `LLMConfig.openrouter_model` (user-selectable).
- **Valid backends:** STT: `moonshine`, `whisper_cpp`, `vosk`, `openrouter`. TTS: `piper`, `kokoro`, `edge_tts`, `openrouter`. LLM: `ollama`, `npu_genie`, `openrouter`, `lmstudio`, `tinkerclaw`.
- **Mode-aware system prompts:** Each voice mode sets a different system prompt length — Local (concise, 128 tokens), Hybrid (medium, 256 tokens), Cloud (rich, 512 tokens). This keeps local model context tight while giving cloud models room for nuanced instructions.
- **Mode-aware pipeline timeouts:** Local mode = 300s (5 min) for tool-calling chains on slow local models. Cloud mode = 60s (1 min). TinkerClaw mode = 180s (3 min, tool execution gaps). Timeouts configured per voice mode in pipeline.py.
- **Session system_prompt updated on mode switch:** When voice_mode changes, the session's `system_prompt` is updated in the DB immediately so the conversation engine picks it up on the next turn.
- **Pipeline init resets to local defaults on reconnect:** When a device reconnects, the pipeline is re-initialized with local defaults (voice_mode 0) regardless of the previous session's mode. The client must re-send `config_update` to restore cloud mode.
- **Per-connection config (deep copy):** Each WebSocket connection gets a deep copy of the global config via `copy.deepcopy()`. This prevents one device's config_update (e.g., switching to cloud mode) from corrupting another device's pipeline config. Without deep copy, two Tab5s connected simultaneously would share the same mutable config object.

## TinkerClaw Integration (Optional Sidecar)

When `voice_mode=3` is active, Dragon delegates all intelligence to the TinkerClaw gateway running on the same machine. Dragon becomes an audio pipe only — STT captures speech, the transcript is forwarded to TinkerClaw, and the response is spoken back via TTS.

- **Port:** 18789, localhost only
- **Service:** `tinkerclaw-gateway.service`
- **Dragon role in mode 3:** Audio pipe only — STT and TTS still run on Dragon, but `ConversationEngine`, `ToolRegistry`, and `MemoryService` are all bypassed. The LLM call goes to TinkerClaw instead of a local/cloud backend.
- **Fallback:** If the gateway is down (connection refused on 18789), Dragon sends an error to Tab5 (same pattern as cloud fallback — auto-revert to Local mode).
- **Config:** `~/.tinkerclaw/tinkerclaw.json`
- **Session continuity:** Dragon's `session_id` is passed as the `user` field in TinkerClaw requests, so TinkerClaw can maintain per-session context.
- **Text bypass:** Text input via WebSocket in mode 3 bypasses ConversationEngine and routes directly through tinkerclaw_llm.py
- **New files:** `dragon_voice/llm/tinkerclaw_llm.py` — LLM backend adapter that forwards requests to the TinkerClaw gateway.

## Rich Media Chat (April 2026)

Dragon renders rich content (code blocks, markdown tables, image URLs) from LLM responses as JPEG images and serves them to Tab5 for inline display in chat.

- **MediaPipeline:** After `llm_done`, `process_response()` scans the full response text. Regex detects code blocks (```lang...```), markdown tables (|col|), and image URLs (.jpg/.png/.gif/.webp). Code blocks are rendered via Pygments `ImageFormatter` (native style, dark theme). Tables are drawn as styled grids (accent orange headers, dark bg) via Pillow. Image URLs are downloaded via aiohttp, resized to 660px max width, JPEG quality 80. Max 3 media items per response.
- **MediaStore:** Disk-backed storage at `/home/radxa/media/`. 24-hour auto-cleanup via hourly `media_cleanup_loop` in `dragon_voice/lifecycle/purge.py` (was inline in `server.py` before #65). 500MB max capacity.
- **`strip_rendered_content()`:** After media items are rendered, code blocks that were converted to images are stripped from the text. The cleaned text is sent as a `text_update` message so Tab5 replaces the last AI bubble.
- **Camera uploads:** Tab5 can send camera photos via `user_media` WebSocket message. Dragon receives the `media_id` from a prior `POST /api/media/upload`, loads the image, and passes it to the LLM for multimodal analysis.
- **Protocol messages (Dragon → Tab5):** `media` (rendered image), `card` (rich card), `audio_clip` (audio player), `text_update` (replace AI bubble text). See `docs/protocol.md` for full spec.
- **Protocol messages (Tab5 → Dragon):** `user_media` (camera photo for multimodal LLM). See `docs/protocol.md`.
- **Dependencies:** Pillow (existing), Pygments>=2.17.0 (new — syntax highlighting). System font `fonts-dejavu-core` required on Dragon for Pygments.
- **Both code paths:** Media detection runs in both ConvEngine and TinkerClaw (voice_mode 3) paths, inside the WS voice handler in `dragon_voice/server.py`. The TinkerClaw path has an early `return` — media detection is placed before it.

## OTA Firmware Endpoints
Dragon serves firmware updates for Tab5 via two endpoints:
- **GET /api/ota/check?current=VERSION** — compares against `/home/radxa/ota/version.json`, returns `{"update":bool,"version":"...","url":"...","sha256":"..."}`
- **GET /api/ota/firmware.bin** — streams `/home/radxa/ota/tinkertab.bin` (8KB chunks)
- **Deploy:** Copy `tinkertab.bin` to `/home/radxa/ota/`, update `version.json` with new version string.

## Key Technical Notes
- **NPU inference (preferred):** Llama 3.2 1B on Genie/HTP achieves ~8 tok/s. Use `npu_genie` backend. See `docs/npu-setup.md`.
- **ARM64 CPU fallback:** Ollama gemma3:4b is ~0.24 tok/s — 30x slower than NPU. Use only when NPU unavailable.
- **Python packages:** Use `pip install --break-system-packages` on Dragon (PEP 668). Rich Media requires `Pygments>=2.17.0` and `fonts-dejavu-core` (`apt install fonts-dejavu-core`).
- **User is radxa, NOT rock:** All service files, paths, and caches must use /home/radxa/
- **PYTHONPATH:** dragon_voice runs as `python3 -m dragon_voice` with PYTHONPATH=/home/radxa
- **Audio rates:** Piper TTS outputs 22050Hz, resampled to 16kHz before sending to Tab5. Tab5 upsamples 16k→48k. OpenRouter TTS outputs 24kHz (resampled to 16kHz before sending).
- **moonshine-voice API:** v0.0.51+ changed download API. Check cache before downloading.
- **Cloud mode backends:** OpenRouter STT and TTS both use `openai/gpt-audio-mini` model via OpenRouter's chat completions API. STT sends base64-encoded WAV audio. TTS streams pcm16 via SSE. API key auto-propagated from `llm.openrouter_api_key` in config.
- **Dictation post-processing:** After dictation ends, `pipeline._post_process_dictation()` sends the full transcript to LLM for title + summary generation, then sends `dictation_summary` message to Tab5.

## Dashboard (port 3500)

The web dashboard is an 11-tab single-page application served by `dashboard.py` on port 3500. It aggregates data from the voice server (3502) and provides a management UI for all Dragon capabilities.

**Proxy architecture:** The dashboard proxies ALL API calls through `/api/proxy/` to the voice server (port 3502). The dashboard itself is a thin frontend — all data lives in the voice server's SQLite database. This means the dashboard has no direct DB access and can be restarted independently without affecting active sessions.

**ngrok access:** Each service has its own ngrok domain — `tinkerclaw-dashboard.ngrok.dev` (dashboard), `tinkerclaw-voice.ngrok.dev` (voice), `tinkerclaw-gateway.ngrok.dev` (TinkerClaw gateway).

| Tab | Description |
|-----|-------------|
| **Overview** | System status, active connections, backend config, CPU/RAM bars (percent-filled visual bars for CPU and RAM usage) |
| **Conversations** | Browse all sessions, view message history, filter by device/status |
| **Chat** | Live SSE-streaming chat interface. Supports stateless mode (direct LLM completion, no session context) for quick queries |
| **Devices** | Registered devices, online/offline status, capabilities, config |
| **Notes** | Notes CRUD, search, audio-to-note, dictation summaries |
| **Logs** | Event log with type/session/device filters |
| **Memory** | Stored facts, semantic search with score bars (visual similarity score for each result), add/delete facts |
| **Documents** | Ingested documents, chunk browser, semantic search across chunks |
| **Tools** | Available tools listing, direct tool execution with dynamic parameter forms generated from each tool's JSON schema definition |
| **OTA** | Firmware update management — upload .bin files, set version metadata, check update status for connected Tab5 devices |
| **Debug** | 55-test E2E test suite runner + Tab5 remote control panel for sending commands and inspecting device state |

## Local LLM Benchmarks (Dragon Q6A, ARM64 CPU via Ollama)

**Re-benchmarked 2026-04-25** with the **10-prompt** gauntlet on the
post-#74/#76/#77 server (parser dialect widening + WS-keepalive-during-
inference + per-tool template wrap).  Previous table from 2026-04-24
used 5 prompts and showed 3 viable models; the 10-prompt + integration-
branch results below are dramatically better because two of the three
systemic blockers identified that day (WS keepalive expiry, empty-reply-
on-tool-fire) are now fixed.

The 10 prompts exercise: G1 datetime, G2 calculator (456×789 = 359,784
— deliberately not a memorized number), G3 store_fact, G4 unit_converter,
G5 timesense, G6 weather, G7 web_search, G8 recall_facts, G9 system_info,
G10 quick_poll.  See `docs/AUDIT.md` "Local-mode gauntlet Round 2 + 3"
for the full per-prompt matrix and the prior 5-prompt baseline.

| Model | Size | Median latency | Correct-tool fires | User-visible replies | Math correct (G2) | Verdict |
|-------|------|----------------|--------------------|----------------------|-------------------|---------|
| **ministral-3:3b** ⭐ | 2.8 GB | 65 s | **7/10** | 5/10 | ✅ 359,784 | **Current default.** Best correct-tool rate; warm conversational replies; only model that fired the real `weather` tool with a useful result. |
| **gemma3:4b** ⭐ | 3.1 GB | 53 s | 6/10 | **7/10** | ✅ tool fired (wrap render gap on G2) | **First alternative.** Best visible-reply rate.  CLAUDE.md previously wrote it off as "OK format, bad answers" — provably wrong now that #77's wrap renders the tool result for it. |
| xLAM-2-1b-fc-r (HF, GGUF) | 1.3 GB | **24 s** | 3/10 | 5/10 | ❌ picked web_search for math | Fastest by 2×.  Half the prompts work cleanly; other half wrong-tool selection (web_search for math), a fourth XML dialect (`[recall query=…]`) the parser doesn't catch, or an honest refusal.  Useful as a tool-picker head in a future dual-model pipeline; bad as a standalone default. |
| qwen3:1.7b | 1.4 GB | 50 s | 0/10 | 5/10 | n/a (ws-reset on G2) | Talks well, never uses tools.  Honest refusals on weather/sysinfo are a feature, not a bug.  OK for chat-only turns; useless for agentic ones. |
| llama3.2:3b | 1.9 GB | 38 s | 1/10 | 4/10 | ✅ 359784 (when it didn't ws-reset) | **Most flaky** — 5/10 connection resets.  When it doesn't reset, math is correct.  Wait for #75-style resilience fixes before using. |
| phi4-mini:latest | 2.5 GB | 92 s | 1/10 | 5/10 | n/a (ws-reset on G2) | Slow AND fakes most tool acks ("Got it" without firing `store_fact`).  Worst combination. |
| qwen2.5:3b (HF) | ~2 GB | 62 s | 1/5 | 1/5 | ❌ 356,184 | (5-prompt baseline only) Fluent, occasionally honest, arithmetic wrong, leaks `[datetime]` templates. |
| hermes3:3b (HF) | 2.0 GB | 52 s | 0/5 | 0 | ❌ 356,664 | (5-prompt baseline only) Talks well, tools zero. |
| qwen3:0.6b (old default) | 0.5 GB | 18 s | 1/5 | 1/5 | ❌ silent | (5-prompt baseline only) Fastest, but emits malformed XML and goes silent on most tool prompts. |
| qwen3:4b | 2.5 GB | 92 s | 0/10 ⚠️ | 0/10 ⚠️ | n/a (no content) | **Phase-1a fixed the connection** — 92 s × 10 prompts no longer triggers P13 eviction.  But the model itself produces no usable content; can't be fixed server-side. |
| qwen3.5:4b | 3.2 GB | 95 s | 0/10 ⚠️ | 0/10 ⚠️ | — | Same as qwen3:4b — keepalive holds, content empty. |
| nemotron-3-nano:4b | 2.6 GB | 92 s | 1/10 ⚠️ | 0/10 ⚠️ | — | Same shape; one tool fired, no visible reply. |
| distil-home-assistant-functiongemma | 2.4 GB | 81 s | **0/10** | **0/10** | ❌ never finished calc | **Worst tested model.**  Despite the "FunctionGemma" branding, emits zero tool markers and dumps raw chain-of-thought fragments truncated mid-thought (`"Let me calculate that. First, I need to multiply 456 by 789. Hmm, that's a big number. Maybe I can use the…"`).  No facts stored, no math answered, no tools fired across all 10 prompts.  `mem_facts_added_last_3min=0` confirms G3 fact-store didn't reach the DB either.  Failure class 3 (fluent hallucinator) at its worst.  Reject. |

⚠️ "0/10 user-visible" on the 4B-class block is post-#76 keepalive — pre-fix
they were 0/5 with P13 eviction errors.  The connection now stays open the
full 92 s, which is the entire point of #76; the model not producing useful
content is upstream of any server fix.

**Three failure classes observed:**
1. **Too small to tool-call** — qwen3 0.6b/1.7b emit malformed XML or give up.
   #74's parser widening helped some FC-trained models, but qwen3-base is
   still in this bucket.
2. **Too slow for keepalive** — 4B-class models take 92 s+ per turn.  #76
   solved the connection-drop part; the model latency itself is the next
   problem (see "When to use what" → NPU path below).
3. **Fluent hallucinators** — llama3.2:3b / hermes3:3b / phi4-mini write
   confident chatty answers that never actually invoke a tool.  Dangerous
   because replies look right at a glance (wrong math, fake timers, recalled
   "memories" that were never stored).  #77's wrap can't help this class
   because there's no real tool result to wrap.

**Current default:** `ministral-3:3b` — set in `dragon_voice/config.yaml`
(`ollama_model: "ministral-3:3b"`).  Post-#74/#76/#77 it scores **7/10
correct-tool fires + 5/10 visible replies** on the 10-prompt gauntlet —
roughly 2× the baseline (3/5 + 3/5 on the 5-prompt 2026-04-24 audit).  Math
correct on G2 (359,784 — the deliberately-not-memorized test).  The known
gap is widget emission (G5 timesense, G10 quick_poll) — works on zero
tested models because of a system-prompt / tool-format mismatch upstream
of model choice.

**When to use what:**
- **Default voice + chat** → `ministral-3:3b`.  Best balance of tool fires
  (7/10), visible replies (5/10), and conversational warmth.
- **Higher reply-rate, slightly slower** → `gemma3:4b`.  7/10 visible
  replies, 1 GB more RAM, 12 s slower per turn.  Worth A/B against ministral
  in real user sessions.
- **Sub-second tool selection (dual-model pipeline, opt-in)** → `xLAM-2-1b-fc-r`
  picks tools fast (24 s median) but doesn't write conversational replies.
  Pair with a small responder model to combine strengths.  Not a default
  candidate alone.  PR #80 ships `backend: "dual"` for this — works
  end-to-end on individual turns but **fails the sustained-gauntlet
  validation gate on Dragon Q6A (11 GB RAM)** because xLAM + ministral
  exceeds practical headroom and Ollama evicts the LRU model under
  pressure.  See `docs/PLAN-dual-model-pipeline.md` "Validation results"
  + LEARNINGS #80 for the matrix.  Default stays single-model on Dragon;
  dual is recommended for ≥ 16 GB hardware only.
- **Agentic chains where reliability matters** → mode 2 (Cloud, OpenRouter
  model picked per `llm_model`) or mode 3 (TinkerClaw Gateway).  Local mode
  with #74/#76/#77 is now usable, but cloud is still better for long chains.
- **Voice latency-critical turns** → the NPU Genie path (`docs/npu-setup.md`)
  is the real escape hatch; until it lands, Local mode is inherently second-
  best on accuracy and third-best on speed behind mode 2/3.

## Current Sprint: Complete (April 2026)

**Phase 0 (Foundation) and Phase 1 (Voice Features) are both complete.** The agentic sprint (tool-calling, memory, documents) is also done. Dragon is a fully functional API-first voice assistant server with agentic capabilities.

### Issues
| # | Title | Status |
|---|-------|--------|
| #16 | Session management infrastructure | DONE (sessions.py, db.py) |
| #17 | Multi-turn conversation engine | DONE (conversation.py, messages.py) |
| #18 | Unified voice + text input | DONE (server.py handles both voice and text) |
| #21 | REST API framework | DONE (api/ package, 47 endpoints — see header) |
| #19 | Notes feature | DONE (notes/ module wired into server.py, API routes registered) |
| — | Cloud mode (OpenRouter STT+TTS) | DONE (openrouter_stt.py, openrouter_tts.py, config_update WS command) |
| — | Dictation mode + post-processing | DONE (dictation in pipeline.py, auto-generated title/summary) |
| #20 | Tab5 SD card storage | DONE (SDMMC 4-bit, FAT32, coexists with WiFi SDIO, notes.js + WAV recordings) |
| #22 | Dashboard conversation viewer | DONE (11-tab SPA: Overview, Conversations, Chat, Devices, Notes, Logs, Memory, Documents, Tools, OTA, Debug) |
| — | Agentic pipeline (tool-calling) | DONE (ToolRegistry, XML parsing, web_search, remember, recall, datetime) |
| — | Memory + RAG | DONE (MemoryService, facts CRUD, document ingestion, semantic search) |
| — | E2E test suite | DONE (55 tests via Debug tab + 29 API tests) |
| — | Settings crash fix (WDT) | DONE (f_getfree cached at boot, esp_task_wdt_reset fed between settings sections) |
| — | Tolerant tool parser | DONE (handles stray `>`, missing `</args>`, small model XML quirks) |
| — | Response timeout (local mode) | DONE (disabled/5 min for local mode, 35s for cloud mode) |
| — | Default local LLM | DONE (ministral-3:3b, ~65 s median, 7/10 correct-tool fires post-#74/#76/#77) — switched from qwen3:0.6b on 2026-04-24 after the 11-model re-benchmark, then upgraded again on 2026-04-25 with the 10-prompt gauntlet on the integration branch.  See Local LLM Benchmarks section + `docs/AUDIT.md` "Local-mode gauntlet Round 2 + 3". |
| — | Rich Media Chat | DONE (MediaPipeline renders code/tables/images as JPEG, MediaStore with 24h cleanup, camera uploads, 44 tests) |

### Architecture Decisions (from scaffolding research)
- **Session != Connection.** Sessions survive disconnects. Device reconnects → resume.
- **Conversation items are append-only.** Never mutate messages.
- **Device is first-class.** Registered with capabilities, tracked online/offline.
- **OpenAI message format** as universal LLM context representation (convert at adapter layer).
- **Notes = sessions tagged type='recording'.** Not a parallel system.
- **Event bus** for decoupled real-time updates (dashboard, notes, skills all subscribe).
- **Scoped config:** global → device → session. More specific wins.
- **aiosqlite** for async SQLite. Single db.py module — no raw SQL scattered across files.
- **Patterns stolen:** LiveKit ChatContext item model, Vocode Transcript metadata, Pipecat Frame taxonomy, StackFlow lifecycle verbs (create/resume/pause/end).

### Schema
See `schema.sql` — 6 tables: devices, sessions, messages, notes, events, config.

### Acceptance Tests (must pass before features)
- Create session → send 5 messages → retrieve full history
- List devices → see which are online
- Hot-swap LLM backend mid-session
- Paginate through old sessions via REST API
- Dashboard shows live conversation via WebSocket events

## API-First Architecture (47 REST endpoints + 1 WebSocket)

_Counted from code: `for f in dragon_voice/api/*.py dragon_voice/notes/api.py; do grep -c 'app.router.add_' "$f"; done | paste -sd+ | bc` → 47. Drifted from "46" to "52" and back — see wave-14 H23._

Dragon is an API-first server. Every capability is accessible via REST so any hardware client can use it.

### REST API Endpoints (/api/v1/*)

| Category | Method | Path | Purpose |
|----------|--------|------|---------|
| **Sessions** | GET | `/api/v1/sessions` | List sessions (filter by device, status) |
| | POST | `/api/v1/sessions` | Create session |
| | GET | `/api/v1/sessions/{id}` | Get session |
| | POST | `/api/v1/sessions/{id}/end` | End session |
| | POST | `/api/v1/sessions/{id}/resume` | Resume paused session |
| | POST | `/api/v1/sessions/{id}/pause` | Pause active session |
| | PATCH | `/api/v1/sessions/{id}` | Update title/system_prompt/metadata |
| | GET | `/api/v1/sessions/{id}/context` | Get formatted LLM context |
| **Messages** | GET | `/api/v1/sessions/{id}/messages` | List messages (paginated) |
| | POST | `/api/v1/sessions/{id}/chat` | SSE streaming LLM chat |
| | GET | `/api/v1/messages/{id}` | Get single message |
| | DELETE | `/api/v1/sessions/{id}/messages` | Purge session messages |
| **Devices** | GET | `/api/v1/devices` | List devices |
| | GET | `/api/v1/devices/{id}` | Get device |
| | PATCH | `/api/v1/devices/{id}` | Update device name/config |
| | DELETE | `/api/v1/devices/{id}` | Remove device |
| **Config** | GET | `/api/v1/config` | List config by scope |
| | GET | `/api/v1/config/{key}` | Get config (with scope resolution) |
| | PUT | `/api/v1/config/{key}` | Set config value |
| | DELETE | `/api/v1/config/{key}` | Delete config key |
| **Events** | GET | `/api/v1/events` | List events (filter by type/session/device) |
| **Media** | POST | `/api/v1/transcribe` | STT: audio bytes → text |
| | POST | `/api/v1/synthesize` | TTS: text → audio bytes |
| | POST | `/api/v1/completions` | Direct LLM (stateless, no session) |
| **System** | GET | `/api/v1/system` | System metrics (CPU, RAM, connections) |
| | GET | `/api/v1/backends` | List available STT/TTS/LLM backends |
| **Tools** | GET | `/api/v1/tools` | List available tools |
| | POST | `/api/v1/tools/{name}/execute` | Execute a tool directly |
| **Memory** | GET | `/api/v1/memory` | List stored facts |
| | POST | `/api/v1/memory` | Store a fact |
| | DELETE | `/api/v1/memory/{id}` | Delete a fact |
| | POST | `/api/v1/memory/search` | Semantic search facts |
| **Documents** | POST | `/api/v1/documents` | Ingest document (chunk + embed) |
| | GET | `/api/v1/documents` | List documents |
| | DELETE | `/api/v1/documents/{id}` | Delete document + chunks |
| | POST | `/api/v1/documents/search` | Semantic search across chunks |
| **Notes** | POST | `/api/notes` | Create note |
| | GET | `/api/notes` | List notes |
| | GET | `/api/notes/{id}` | Get note |
| | PUT | `/api/notes/{id}` | Update note |
| | DELETE | `/api/notes/{id}` | Delete note |
| | POST | `/api/notes/search` | Semantic search notes |
| | POST | `/api/notes/from-audio` | Create note from audio |
| **OTA** | GET | `/api/ota/check` | Check firmware updates |
| | GET | `/api/ota/firmware.bin` | Download firmware |
| **Rich Media** | GET | `/api/media/{id}` | Serve rendered media file (JPEG/PNG/WAV), Cache-Control 1h |
| | POST | `/api/media/upload` | Accept BMP/JPEG from Tab5 camera, convert+resize via Pillow, return media_id |

### Agentic Pipeline

Dragon is an agent, not just a voice parrot. The LLM can call tools:
- **Tool-calling:** LLM outputs `<tool>name</tool><args>{...}</args>` → parsed → executed → result injected → LLM continues
- **Three accepted dialects** (see `dragon_voice/tools/registry.py` module docstring + LEARNINGS #79):
  1. **Legacy** — `<tool>NAME</tool><args>{json}</args>` (TinkerBox system-prompt format; ministral, gemma3 emit this).  Tolerates xLAM bracket quirks (`[tool>`, `<tool]`, `[tool]`).
  2. **Standard** — `<tool_call>{"name": "...", "arguments": {...}}</tool_call>` (industry-typical FC fine-tunes; Qwen-FC, Gemma-FC, distil-* all emit this regardless of system prompt).
  3. **Bracketed-name** — `[NAME]{json}</NAME>` or `[NAME]UPPERCASE_IDENT()` (xLAM quirk surfaced in #82).  Gated on `NAME` being in the registered tool set so prose like `[note]` in chat doesn't false-fire.
- **Built-in tools:** `web_search` (SearXNG, self-hosted on port 8888, returns up to 44 results), `remember` (store fact), `recall` (search memory), `datetime`, plus additional tools (10 total)
- **Compact tool format:** For local models with limited context, tool definitions are sent in a compact XML format to minimize token usage
- **Memory-augmented context:** Before every LLM call, relevant facts + document chunks injected into system prompt
- **WebSocket events:** `tool_call` and `tool_result` events sent to connected clients during tool execution
- **Max 3 tool calls per turn** to prevent infinite loops
- **Empty-reply guard (`tools/response_wrap.py`, #77):** Some FC-trained models (xLAM, distil-functiongemma, LFM2.5-Nova) emit a tool call and stop — leaving the user-visible text empty after the parser strips the markup.  When that happens AND at least one tool fired, Dragon synthesizes a one-line natural-language ack from the tool result (per-tool template library, no extra LLM call).  See `dragon_voice/tools/response_wrap.py` for the per-tool wrap functions and the `synthesize_wrap` entrypoint.  PR #79 widened the trigger from "strict empty" to "no useful text" (residual XML / bracket noise also counts) so models like gemma3 that emit a stripped-empty `<` after the markup also get the wrap.
- **WS keepalive during inference (`server.py: _ws_keepalive_during_inference`, #76):** Local-mode 4 B-class models routinely take 60-90 s per turn.  Tab5's WebSocket library times out after ~30 s without a PONG and triggers a reconnect, which hits server.py's P13 "Device already has connection" guard, which evicts the in-flight LLM stream — net result: empty reply on every slow turn.  The keepalive context manager fires `ws.ping()` every 5 s while ConversationEngine is generating, well under any client's PONG-watch window.  Wired into the three slow paths: TC text bypass, local text via ConvEngine, and the vision/multimodal path.  See LEARNINGS #78 for the original eviction analysis that motivated this.

### Memory Service
Facts are stored with Ollama embeddings (`nomic-embed-text`, 768-dim vectors) for semantic search. Store facts via the `remember` tool (LLM-initiated) or `POST /api/v1/memory` (REST API). All stored facts are auto-recalled before every LLM call — relevant facts are injected into the system prompt via cosine similarity search against the user's query embedding.

### Document Service
Text documents are chunked (512 tokens per chunk, 50 token overlap between chunks), embedded with `nomic-embed-text`, and stored in SQLite with `sqlite-vec` for vector search. Search via `POST /api/v1/documents/search` returns ranked chunks by cosine similarity. Documents provide long-term knowledge that augments the LLM's context alongside memory facts.

### Tools
10 built-in tools:
- **web_search** — SearXNG metasearch (self-hosted on port 8888, up to 44 results, DDG fallback if SearXNG is down)
- **remember** — store a fact in the memory service
- **recall** — semantic search over stored facts
- **datetime** — current date/time
- Plus 6 additional tools registered via the ToolRegistry

The LLM uses XML markers to invoke tools: `<tool>name</tool><args>{"key":"value"}</args>`. The tool parser is tolerant of small model quirks — it handles stray `>` after `</args>`, missing closing tags, and other formatting issues common with smaller local models (e.g. qwen3:1.7b). For local models, tool definitions use a compact format to minimize context token usage.

### Embedding Model
All embeddings (memory facts, document chunks, search queries) use **Ollama nomic-embed-text** (768-dimensional vectors). Runs locally on Dragon via Ollama on port 11434. No cloud API required.

### File Structure

Refactor note (umbrella #65, closed 2026-04-24): the 2,747-LOC monolithic
`server.py` was decomposed into four sibling packages — `middleware/`
(request-filter concerns), `handlers/` (diagnostic + status + config
endpoints), `lifecycle/` (boot/shutdown/monitors), and the slimmed
`server.py` itself (now 1,803 LOC — holds the `VoiceServer` class +
`create_app` wiring + the big WS-voice-handler family).  The extracted
modules take deps explicitly (server handle or specific args), so each
can be unit-tested without instantiating the full server.

```
schema.sql            — Database schema (9 tables: 6 foundation + 3 memory)
dashboard.py          — Web dashboard (port 3500, aggregates device state + voice state)
dragon_voice/         — Voice pipeline package (port 3502)
  __init__.py         — Package init
  __main__.py         — Entry point: python3 -m dragon_voice
  server.py           — VoiceServer class + create_app wiring + WS-voice handler family
                        (register / text / user_media / disconnect / audio + event hooks).
                        1,803 LOC after #65 decomposition.
  pipeline.py         — STT→LLM→TTS orchestration with VAD + dictation + post-processing
  conversation.py     — Multi-turn ConversationEngine with tool-calling + memory-augmented context
  sessions.py         — SessionManager (create/resume/pause/end lifecycle)
  messages.py         — MessageStore (append-only, LLM context builder, includes tool messages)
  db.py               — Async SQLite layer (aiosqlite, WAL mode, full CRUD)
  memory.py           — MemoryService: facts + documents + RAG with Ollama embeddings
  config.py           — Config dataclasses (incl. ToolsConfig, MemoryConfig)
  config.yaml         — Default configuration
  middleware/         — aiohttp request middleware (each stateless, explicit deps)
    __init__.py       — Re-exports submodules
    cors.py           — handle_cors + DEFAULT_ALLOWED_ORIGINS (SEC12)
    security_headers.py — handle_security_headers + SECURITY_HEADERS (W14-M06)
    auth.py           — handle_auth + PUBLIC_PREFIXES (Wave 13 C2 bearer-token gate)
    rate_limit.py     — handle_rate_limit + DEFAULT_RATE_LIMIT_RULES (W15-H01, W15-H06)
  handlers/           — HTTP endpoint handlers extracted from VoiceServer
    __init__.py       — Re-exports submodules
    debug.py          — handle_debug_mem + handle_debug_widget_{chart,prompt,card,media}
                        (tracemalloc/RSS/gc probe + audit B2/B5/B6/K3 widget emitters)
    status.py         — handle_status (HTML) + handle_health (JSON liveness)
    config_api.py     — handle_get_config (redacted dump) + handle_set_config (hot-reload + pipeline backend swap, A06 lock-guarded)
  lifecycle/          — Boot / shutdown / long-running monitors
    __init__.py       — Re-exports submodules
    monitors.py       — get_rss_mb, get_cpu_temp, memory_monitor_loop (A04 — 5-min RSS/temp/FD sample + pipeline drain on crit)
    purge.py          — periodic_purge_loop (US-DQ14 message retention) + media_cleanup_loop (W13-H3 mid-sleep cancel fix preserved)
    startup.py        — run_startup(server, app): DB → sessions → memory + tools → surfaces → conversation → REST routes → notes → MCP → periodic tasks
    shutdown.py       — run_shutdown(server, app): cancel+await monitors (W14-M09) → drain pipelines → release backend pool (W15-C01) → close HTTP clients (W14-H12)
  api/                — Modular REST API package (47 endpoints, counted from code)
    __init__.py       — setup_all_routes() entry point
    utils.py          — Shared helpers (json_error, pagination)
    sessions.py       — Session CRUD + lifecycle routes
    messages.py       — Message listing + SSE chat routes
    devices.py        — Device CRUD routes
    config_routes.py  — Config CRUD + delete routes
    events.py         — Events listing with device_id filter
    synthesize.py     — TTS synthesis + STT transcription + OTA routes
    completions.py    — Direct LLM completion (stateless)
    system.py         — System metrics + backend listing
    tools.py          — Tool listing + execution routes
    memory_routes.py  — Memory facts CRUD + search routes
    documents.py      — Document ingest + listing + search routes
    media_routes.py   — GET /api/media/{id} (serve), POST /api/media/upload (Tab5 camera)
  tools/              — Tool-calling infrastructure (~15 tools)
    __init__.py       — Exports ToolRegistry, Tool
    base.py           — Tool abstract base class
    registry.py       — ToolRegistry: register, parse XML markers, execute
    web_search.py     — SearXNG-backed web search (falls back to DuckDuckGo)
    memory_tools.py   — StoreFactTool + RecallFactsTool + ForgetFactTool (G9 confirm-gated)
    datetime_tool.py  — Current date/time tool
    calculator_tool.py, unit_converter_tool.py, weather_tool.py
    system_tool.py, stock_ticker_tool.py
    timesense_tool.py — Pomodoro + widget_live emitter (registered after SurfaceManager)
    quick_poll_tool.py — Wave 12 declarative widget-skill reference
    note_tool.py      — NoteTool (registered after NotesService)
  stt/                — STT backends (moonshine, whisper_cpp, vosk, openrouter)
  tts/                — TTS backends (piper, kokoro, edge_tts, openrouter)
  llm/                — LLM backends (ollama, openrouter, lmstudio, npu_genie, tinkerclaw)
  notes/              — Notes module (CRUD + search + audio ingestion)
  media/              — Rich media rendering package
    __init__.py       — Package exports (MediaStore, MediaPipeline)
    store.py          — MediaStore: disk-backed media file storage, 24h auto-cleanup, 500MB max
    pipeline.py       — MediaPipeline: detects code/tables/image URLs in LLM output, renders JPEG via Pygments/Pillow
    url_signer.py     — MediaUrlSigner: HMAC-signed + time-bounded /api/media/{id} URLs (W14-H04)
  surfaces/           — Tab5 widget-surface abstraction (widget_live/card/list/chart/media/prompt)
  mcp/                — Model Context Protocol client + bridge
tests/                — Test suite (112 functions across 14 files in CI named-set; 153 tests
                        collected when running pytest tests/ directly excluding the audit/
                        async suite; the test_api_e2e.py CLI runner contributes another 29
                        live-Dragon scenarios that don't run in CI)
  test_api_e2e.py               — 29 live-device tests (local-only, not CI)
  test_e2e_dragon.py            — Dragon end-to-end (local-only, not CI)
  test_auth_middleware.py       — 6 tests for bearer-token gate (CI)
  test_rate_limit.py            — 4 tests for per-IP+path throttle (CI)
  test_security_headers.py      — 3 tests for CSP/XFO/nosniff stamping (CI)
  test_debug_handlers.py        — 7 tests for handlers/debug.py (CI, added in #67)
  test_lifecycle_monitors.py    — 5 tests for lifecycle/monitors.py (CI, added in #68)
  test_status_handlers.py       — 3 tests for handlers/status.py (CI, added in #69)
  test_config_api_handlers.py   — 4 tests for handlers/config_api.py (CI, added in #69)
  test_media_store.py, test_media_pipeline.py, test_media_url_signer.py,
  test_proxy_image_ssrf.py, test_config_redact.py, test_session_cas.py,
  test_mcp_bridge.py, test_notes_db_async.py, test_backend_pool.py,
  test_media_fd_leak.py, test_foundation.py       — all in CI named set
docs/
  protocol.md         — WebSocket protocol spec (Tab5 ↔ Dragon)
  npu-setup.md        — Qualcomm NPU / QAIRT SDK setup guide
  AUDIT.md / AUDIT-WAVE-15.md / WAVE-14-PROGRESS.md / WAVE-15-PROGRESS.md
                      — Wave audit tracking; note that file:line citations for
                        server.py from before 2026-04-24 pre-date the #65
                        refactor and may need mapping to the new module paths
  SKILL_AUTHORING.md  — Skill SDK reference (uses tools/quick_poll_tool.py as example)
LEARNINGS.md          — Institutional knowledge (MANDATORY reading)
```

**Note on legacy files:** `dragon_server.py` (CDP browser streaming on port 3501) and `udp_streamer.py` (UDP JPEG streaming) were retired on the Tab5 side in #155 ("voice-first is the product").  If copies still exist on a deployed Dragon they're no longer wired up by systemd; the active dashboard now aggregates state from `dragon_voice` directly.

## Testing

### E2E API Tests (`tests/test_api_e2e.py`)
- **29 API tests** — all passing
  - **14 single-step tests:** Basic CRUD operations (create session, list devices, store fact, etc.)
  - **8 multi-step tests:** Sequences requiring state (create session → send messages → retrieve history, etc.)
  - **7 complex chained tests:** Full workflows across multiple subsystems (session + chat + memory + tools, etc.)

### Media Tests
- **41 media tests** — all passing
  - `tests/test_media_store.py` — 12 unit tests for MediaStore (disk storage, cleanup, capacity limits)
  - `tests/test_media_pipeline.py` — 29 unit tests for MediaPipeline (code block detection, table rendering, image URL handling, strip logic)

Aggregate pytest run (excluding `tests/audit/` which needs pytest-asyncio): **153 tests collected, 153 passing** (April 2026, post-wave-15 + #79/#85 follow-up).  Verify with `python3 -m pytest tests/ -q --ignore=tests/audit`.  The CI named-set is a tighter subset — 14 files, 112 functions — picked so each can run without a live server; everything else is local-only.

### Dashboard Debug Tab E2E Suite
- **55 tests** — runnable from the Debug tab in the dashboard
  - Covers all REST endpoints, WebSocket flows, tool execution, memory ops, and cross-subsystem chains
  - Includes Tab5 remote control for sending commands and inspecting device state

### Device Tests (on-device against live Dragon)
- **25/26 API endpoint tests** passing (full REST surface coverage)
- **10/10 compound story tests** (multi-step workflows: session lifecycle, chat + memory, etc.)
- **8/8 complex chain tests** (cross-subsystem: session → chat → tools → memory → documents)

### Tool Calling Quality
- **12 scenarios tested locally** against qwen3:1.7b (default) and qwen3:4b
- Covers: web search, memory store/recall, datetime, multi-tool chains, edge cases

Run tests against a live Dragon instance:
```bash
# From workstation (Dragon must be running on 192.168.1.91:3502)
python3 tests/test_api_e2e.py
```
