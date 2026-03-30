# TinkerBox — Dragon Server Stack (THE BRAIN)

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
1. **Issue first** — Create a GitHub issue before starting work (`gh issue create`)
2. **Branch** — Create a feature/fix branch from main
3. **Commit with issue ref** — Every commit must reference an issue (`refs #N` or `closes #N`)
4. **Push and merge** — Push to origin, merge to main

## Dragon Access
- **Host:** 192.168.1.89 (static IP on LAN)
- **User:** radxa
- **Password:** radxa
- **SSH:** `sshpass -p 'radxa' ssh radxa@192.168.1.89`
- **OS:** Debian (Radxa Zero 3W, ARM64)

## Service Map
| Service | Port | SystemD Unit | Description |
|---------|------|-------------|-------------|
| Dashboard | 3500 | tinkerclaw-dashboard | Web UI for device management |
| Dragon CDP | 3501 | tinkerclaw | CDP browser streaming + touch relay |
| Voice | 3502 | tinkerclaw-voice | STT/LLM/TTS voice pipeline |
| mDNS | — | tinkerclaw-mdns | Advertises _tinkerclaw._tcp |
| Chromium | 9222 | (launched by tinkerclaw) | CDP target browser |
| Ollama | 11434 | ollama | Local LLM inference (CPU, slow) |
| NPU Genie | — | (via voice pipeline) | Llama 3.2 1B on QCS6490 HTP (~8 tok/s) |

## Deploy
```bash
# Sync code to Dragon
sshpass -p 'radxa' scp -r dragon_voice/ radxa@192.168.1.89:/home/radxa/
sshpass -p 'radxa' scp dashboard.py radxa@192.168.1.89:/home/radxa/

# Restart services
sshpass -p 'radxa' ssh radxa@192.168.1.89 "echo 'radxa' | sudo -S systemctl restart tinkerclaw-voice"
```

## Key Technical Notes
- **NPU inference (preferred):** Llama 3.2 1B on Genie/HTP achieves ~8 tok/s. Use `npu_genie` backend. See `docs/npu-setup.md`.
- **ARM64 CPU fallback:** Ollama gemma3:4b is ~0.24 tok/s — 30x slower than NPU. Use only when NPU unavailable.
- **Python packages:** Use `pip install --break-system-packages` on Dragon (PEP 668)
- **User is radxa, NOT rock:** All service files, paths, and caches must use /home/radxa/
- **PYTHONPATH:** dragon_voice runs as `python3 -m dragon_voice` with PYTHONPATH=/home/radxa
- **Audio rates:** Piper TTS outputs 22050Hz, resampled to 16kHz before sending to Tab5. Tab5 upsamples 16k→48k.
- **moonshine-voice API:** v0.0.51+ changed download API. Check cache before downloading.

## Current Sprint: Phase 0 — The Foundation (March 2026)

**Build order:** Sessions → Conversation Engine → Unified Voice+Text → REST API → Notes → SD Card → Dashboard Viewer

### Issues
| # | Title | Status |
|---|-------|--------|
| #16 | Session management infrastructure | DONE (sessions.py, db.py) |
| #17 | Multi-turn conversation engine | DONE (conversation.py, messages.py) |
| #18 | Unified voice + text input | DONE (server.py handles both voice and text) |
| #21 | REST API framework | DONE (api.py, /api/v1/ routes) |
| #19 | Notes feature | BLOCKED on #16, #17 (schema ready, notes/ module stubbed) |
| #20 | Tab5 SD card storage | BLOCKED on #19 |
| #22 | Dashboard conversation viewer | BLOCKED on #21 |

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

## File Structure
```
schema.sql            — Foundation database schema (6 tables)
dragon_server.py      — CDP streaming + touch WebSocket (port 3501)
dashboard.py          — Web dashboard (port 3500, aggregates 3501+3502)
udp_streamer.py       — UDP JPEG streaming for low-latency display
dragon_voice/         — Voice pipeline package (port 3502)
  __init__.py         — Package init
  __main__.py         — Entry point: python3 -m dragon_voice
  server.py           — aiohttp WebSocket server + HTTP endpoints
  pipeline.py         — STT→LLM→TTS orchestration with VAD
  conversation.py     — Multi-turn ConversationEngine (DB-backed context)
  sessions.py         — SessionManager (create/resume/pause/end lifecycle)
  messages.py         — MessageStore (append-only, LLM context builder)
  db.py               — Async SQLite layer (aiosqlite, WAL mode)
  api.py              — REST API v1 routes (/api/v1/*)
  config.py           — Config dataclasses with YAML + env var loading
  config.yaml         — Default configuration
  stt/                — STT backends (moonshine, whisper_cpp, vosk)
  tts/                — TTS backends (piper, kokoro, edge_tts)
  llm/                — LLM backends (ollama, openrouter, lmstudio, npu_genie)
  notes/              — Notes module (db, service, api) — stubbed, not yet wired
tests/                — E2E tests (run on Dragon)
  test_foundation.py  — Foundation module tests
  test_multiturn_live.py — Multi-turn conversation tests
  test_resume_live.py — Session resume tests
docs/
  protocol.md         — WebSocket protocol spec (Tab5 ↔ Dragon)
  npu-setup.md        — Qualcomm NPU / QAIRT SDK setup guide
LEARNINGS.md          — Institutional knowledge (MANDATORY reading)
```
