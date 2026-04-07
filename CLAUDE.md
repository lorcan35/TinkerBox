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
| Ollama | 11434 | ollama | Local LLM inference (CPU, slow) |
| NPU Genie | — | (via voice pipeline) | Llama 3.2 1B on QCS6490 HTP (~8 tok/s) |
| ngrok | 443 (ext) | tinkerclaw-ngrok | tinkerbox.ngrok.dev → voice server |

## Deploy
```bash
# Sync code to Dragon (includes new STT/TTS backends + notes module)
sshpass -p 'radxa' scp -r dragon_voice/ radxa@192.168.1.89:/home/radxa/
sshpass -p 'radxa' scp dashboard.py radxa@192.168.1.89:/home/radxa/
sshpass -p 'radxa' scp schema.sql radxa@192.168.1.89:/home/radxa/

# Restart services
sshpass -p 'radxa' ssh radxa@192.168.1.89 "echo 'radxa' | sudo -S systemctl restart tinkerclaw-voice"
```

## Cloud Mode
- **What:** Tab5 sends `{"type":"config_update","cloud_mode":true}` over WebSocket. Dragon hot-swaps STT and TTS backends to `openrouter` (no restart needed).
- **Backends:** Both STT and TTS use `openai/gpt-audio-mini` via OpenRouter's chat completions API. STT sends base64 WAV, TTS streams pcm16 via SSE at 24kHz.
- **Config propagation:** API key auto-propagated from `llm.openrouter_api_key` to `stt.openrouter_api_key` and `tts.openrouter_api_key` at config load time. No duplicate key config needed.
- **Toggle off:** Reverts to local backends (Moonshine STT + Piper TTS). Dragon sends `config_update` ACK with applied backend names and `cloud_mode` state.
- **Config fields:** `STTConfig.openrouter_api_key`, `STTConfig.openrouter_url`, `TTSConfig.openrouter_api_key`, `TTSConfig.openrouter_url`, `TTSConfig.openrouter_voice` (default "alloy").
- **Valid backends:** STT: `moonshine`, `whisper_cpp`, `vosk`, `openrouter`. TTS: `piper`, `kokoro`, `edge_tts`, `openrouter`.

## Key Technical Notes
- **NPU inference (preferred):** Llama 3.2 1B on Genie/HTP achieves ~8 tok/s. Use `npu_genie` backend. See `docs/npu-setup.md`.
- **ARM64 CPU fallback:** Ollama gemma3:4b is ~0.24 tok/s — 30x slower than NPU. Use only when NPU unavailable.
- **Python packages:** Use `pip install --break-system-packages` on Dragon (PEP 668)
- **User is radxa, NOT rock:** All service files, paths, and caches must use /home/radxa/
- **PYTHONPATH:** dragon_voice runs as `python3 -m dragon_voice` with PYTHONPATH=/home/radxa
- **Audio rates:** Piper TTS outputs 22050Hz, resampled to 16kHz before sending to Tab5. Tab5 upsamples 16k→48k. OpenRouter TTS outputs 24kHz (resampled to 16kHz before sending).
- **moonshine-voice API:** v0.0.51+ changed download API. Check cache before downloading.
- **Cloud mode backends:** OpenRouter STT and TTS both use `openai/gpt-audio-mini` model via OpenRouter's chat completions API. STT sends base64-encoded WAV audio. TTS streams pcm16 via SSE. API key auto-propagated from `llm.openrouter_api_key` in config.
- **Dictation post-processing:** After dictation ends, `pipeline._post_process_dictation()` sends the full transcript to LLM for title + summary generation, then sends `dictation_summary` message to Tab5.

## Current Sprint: Phase 1 — Voice Features (April 2026)

**Phase 0 (Foundation) is complete.** Phase 1 adds cloud mode, dictation, and wires up notes.

### Issues
| # | Title | Status |
|---|-------|--------|
| #16 | Session management infrastructure | DONE (sessions.py, db.py) |
| #17 | Multi-turn conversation engine | DONE (conversation.py, messages.py) |
| #18 | Unified voice + text input | DONE (server.py handles both voice and text) |
| #21 | REST API framework | DONE (api.py, /api/v1/ routes) |
| #19 | Notes feature | DONE (notes/ module wired into server.py, API routes registered) |
| — | Cloud mode (OpenRouter STT+TTS) | DONE (openrouter_stt.py, openrouter_tts.py, config_update WS command) |
| — | Dictation mode + post-processing | DONE (dictation in pipeline.py, auto-generated title/summary) |
| #20 | Tab5 SD card storage | DONE (SDMMC 4-bit, FAT32, coexists with WiFi SDIO, notes.js + WAV recordings) |
| #22 | Dashboard conversation viewer | DONE (6-tab SPA: Overview, Conversations, Chat, Devices, Notes, Logs) |

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
  server.py           — aiohttp WebSocket server + HTTP endpoints + config_update handler
  pipeline.py         — STT→LLM→TTS orchestration with VAD + dictation + post-processing
  conversation.py     — Multi-turn ConversationEngine (DB-backed context)
  sessions.py         — SessionManager (create/resume/pause/end lifecycle)
  messages.py         — MessageStore (append-only, LLM context builder)
  db.py               — Async SQLite layer (aiosqlite, WAL mode)
  api.py              — REST API v1 routes (/api/v1/*)
  config.py           — Config dataclasses with YAML + env var loading (incl. OpenRouter STT/TTS fields)
  config.yaml         — Default configuration
  stt/                — STT backends (moonshine, whisper_cpp, vosk, openrouter)
    base.py           — STTBackend abstract base class
    moonshine_stt.py  — Moonshine local STT
    whisper_cpp.py    — whisper.cpp local STT
    vosk_stt.py       — Vosk local STT
    openrouter_stt.py — Cloud STT via OpenRouter gpt-audio-mini (base64 WAV → text)
  tts/                — TTS backends (piper, kokoro, edge_tts, openrouter)
    base.py           — TTSBackend abstract base class
    piper_tts.py      — Piper local TTS (22050Hz)
    kokoro_tts.py     — Kokoro local TTS
    edge_tts_backend.py — Edge TTS (Microsoft cloud)
    openrouter_tts.py — Cloud TTS via OpenRouter gpt-audio-mini (SSE pcm16 @ 24kHz)
  llm/                — LLM backends (ollama, openrouter, lmstudio, npu_genie)
  notes/              — Notes module (wired into server.py, API routes registered)
    db.py             — NotesDB (SQLite persistence)
    service.py        — NotesService (business logic)
    api.py            — Notes REST API routes (setup_routes → aiohttp app)
tests/                — E2E tests (run on Dragon)
  test_foundation.py  — Foundation module tests
  test_multiturn_live.py — Multi-turn conversation tests
  test_resume_live.py — Session resume tests
docs/
  protocol.md         — WebSocket protocol spec (Tab5 ↔ Dragon)
  npu-setup.md        — Qualcomm NPU / QAIRT SDK setup guide
LEARNINGS.md          — Institutional knowledge (MANDATORY reading)
```
