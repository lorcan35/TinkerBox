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
- **Host:** 192.168.1.91 (static IP on LAN)
- **User:** radxa
- **Password:** radxa
- **SSH:** `sshpass -p 'radxa' ssh radxa@192.168.1.91`
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
sshpass -p 'radxa' scp -r dragon_voice/ radxa@192.168.1.91:/home/radxa/
sshpass -p 'radxa' scp dashboard.py radxa@192.168.1.91:/home/radxa/
sshpass -p 'radxa' scp schema.sql radxa@192.168.1.91:/home/radxa/

# Restart services
sshpass -p 'radxa' ssh radxa@192.168.1.91 "echo 'radxa' | sudo -S systemctl restart tinkerclaw-voice"
```

## Three-Tier Voice Mode
Tab5 sends `{"type":"config_update","voice_mode":0|1|2,"llm_model":"..."}`. Dragon hot-swaps backends:

| Mode | voice_mode | STT | LLM | TTS |
|------|-----------|-----|-----|-----|
| **Local** | 0 | Moonshine | Local (npu_genie/ollama) | Piper (22050Hz) |
| **Hybrid** | 1 | OpenRouter gpt-audio-mini | Local (unchanged) | OpenRouter gpt-audio-mini (24kHz) |
| **Full Cloud** | 2 | OpenRouter gpt-audio-mini | OpenRouter (user-selected model) | OpenRouter gpt-audio-mini (24kHz) |

- **LLM Model Selection:** `llm_model` field selects cloud model: `anthropic/claude-3-haiku`, `anthropic/claude-sonnet-4-20250514`, `openai/gpt-4o-mini`. Stored in `LLMConfig.openrouter_model`.
- **API Key:** Auto-propagated from `llm.openrouter_api_key` to `stt.openrouter_api_key` and `tts.openrouter_api_key`. Validated before swap — rejects with error if empty.
- **Auto-Fallback:** If cloud STT/TTS fails (timeout, API error), pipeline auto-falls back to local (Moonshine/Piper) for that request AND sends `config_update` with `error` field to Tab5 → auto-reverts to Local mode.
- **Backward compat:** Old `cloud_mode` boolean still accepted (maps to voice_mode 0 or 2).
- **Config fields:** `LLMConfig.local_backend` (remembers original for fallback), `LLMConfig.openrouter_model` (user-selectable).
- **Valid backends:** STT: `moonshine`, `whisper_cpp`, `vosk`, `openrouter`. TTS: `piper`, `kokoro`, `edge_tts`, `openrouter`. LLM: `ollama`, `npu_genie`, `openrouter`, `lmstudio`.

## OTA Firmware Endpoints
Dragon serves firmware updates for Tab5 via two endpoints:
- **GET /api/ota/check?current=VERSION** — compares against `/home/radxa/ota/version.json`, returns `{"update":bool,"version":"...","url":"...","sha256":"..."}`
- **GET /api/ota/firmware.bin** — streams `/home/radxa/ota/tinkertab.bin` (8KB chunks)
- **Deploy:** Copy `tinkertab.bin` to `/home/radxa/ota/`, update `version.json` with new version string.

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

## Dashboard (port 3500)

The web dashboard is a 9-tab single-page application served by `dashboard.py` on port 3500. It aggregates data from the voice server (3502) and provides a management UI for all Dragon capabilities.

**Proxy architecture:** The dashboard proxies ALL API calls through `/api/proxy/` to the voice server (port 3502). The dashboard itself is a thin frontend — all data lives in the voice server's SQLite database. This means the dashboard has no direct DB access and can be restarted independently without affecting active sessions.

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

## Local LLM Benchmarks (Dragon Q6A, ARM64 CPU via Ollama)

| Model | tok/s | Tool Calling | RAM |
|-------|-------|-------------|-----|
| qwen3:0.6b | 11.8 | Untested | 0.5GB |
| qwen3:1.7b | 7.1 | Good (current default) | 1.4GB |
| qwen3:4b | 3.0 | Excellent (97.5%) | 2.5GB |
| gemma3:4b | 3.4 | OK format, bad answers | 3.3GB |

**Current default:** `qwen3:1.7b` — best balance of speed and tool-calling accuracy for the Dragon's ARM64 CPU. Tool calling quality tested across 12 scenarios (web search, memory store/recall, datetime, multi-tool chains).

## Current Sprint: Complete (April 2026)

**Phase 0 (Foundation) and Phase 1 (Voice Features) are both complete.** The agentic sprint (tool-calling, memory, documents) is also done. Dragon is a fully functional API-first voice assistant server with agentic capabilities.

### Issues
| # | Title | Status |
|---|-------|--------|
| #16 | Session management infrastructure | DONE (sessions.py, db.py) |
| #17 | Multi-turn conversation engine | DONE (conversation.py, messages.py) |
| #18 | Unified voice + text input | DONE (server.py handles both voice and text) |
| #21 | REST API framework | DONE (api/ package, 50 endpoints) |
| #19 | Notes feature | DONE (notes/ module wired into server.py, API routes registered) |
| — | Cloud mode (OpenRouter STT+TTS) | DONE (openrouter_stt.py, openrouter_tts.py, config_update WS command) |
| — | Dictation mode + post-processing | DONE (dictation in pipeline.py, auto-generated title/summary) |
| #20 | Tab5 SD card storage | DONE (SDMMC 4-bit, FAT32, coexists with WiFi SDIO, notes.js + WAV recordings) |
| #22 | Dashboard conversation viewer | DONE (9-tab SPA: Overview, Conversations, Chat, Devices, Notes, Logs, Memory, Documents, Tools) |
| — | Agentic pipeline (tool-calling) | DONE (ToolRegistry, XML parsing, web_search, remember, recall, datetime) |
| — | Memory + RAG | DONE (MemoryService, facts CRUD, document ingestion, semantic search) |
| — | E2E test suite | DONE (29 tests: 14 single-step, 8 multi-step, 7 complex chained) |
| — | Settings crash fix (WDT) | DONE (f_getfree cached at boot, esp_task_wdt_reset fed between settings sections) |
| — | Tolerant tool parser | DONE (handles stray `>`, missing `</args>`, small model XML quirks) |
| — | Response timeout (local mode) | DONE (disabled/5 min for local mode, 35s for cloud mode) |
| — | Default local LLM | DONE (qwen3:1.7b set as default, 7.1 tok/s, good tool calling) |

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

## API-First Architecture (50 REST endpoints + 1 WebSocket)

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

### Agentic Pipeline

Dragon is an agent, not just a voice parrot. The LLM can call tools:
- **Tool-calling:** LLM outputs `<tool>name</tool><args>{...}</args>` → parsed → executed → result injected → LLM continues
- **Built-in tools:** `web_search` (DuckDuckGo), `remember` (store fact), `recall` (search memory), `datetime`
- **Memory-augmented context:** Before every LLM call, relevant facts + document chunks injected into system prompt
- **WebSocket events:** `tool_call` and `tool_result` events sent to connected clients during tool execution
- **Max 3 tool calls per turn** to prevent infinite loops

### Memory Service
Facts are stored with Ollama embeddings (`nomic-embed-text`, 768-dim vectors) for semantic search. Store facts via the `remember` tool (LLM-initiated) or `POST /api/v1/memory` (REST API). All stored facts are auto-recalled before every LLM call — relevant facts are injected into the system prompt via cosine similarity search against the user's query embedding.

### Document Service
Text documents are chunked (512 tokens per chunk, 50 token overlap between chunks), embedded with `nomic-embed-text`, and stored in SQLite with `sqlite-vec` for vector search. Search via `POST /api/v1/documents/search` returns ranked chunks by cosine similarity. Documents provide long-term knowledge that augments the LLM's context alongside memory facts.

### Tools
4 built-in tools:
- **web_search** — DuckDuckGo search (no API key required)
- **remember** — store a fact in the memory service
- **recall** — semantic search over stored facts
- **datetime** — current date/time

The LLM uses XML markers to invoke tools: `<tool>name</tool><args>{"key":"value"}</args>`. The tool parser is tolerant of small model quirks — it handles stray `>` after `</args>`, missing closing tags, and other formatting issues common with smaller local models (e.g. qwen3:1.7b).

### Embedding Model
All embeddings (memory facts, document chunks, search queries) use **Ollama nomic-embed-text** (768-dimensional vectors). Runs locally on Dragon via Ollama on port 11434. No cloud API required.

### File Structure
```
schema.sql            — Database schema (9 tables: 6 foundation + 3 memory)
dragon_server.py      — CDP streaming + touch WebSocket (port 3501)
dashboard.py          — Web dashboard (port 3500, aggregates 3501+3502)
udp_streamer.py       — UDP JPEG streaming for low-latency display
dragon_voice/         — Voice pipeline package (port 3502)
  __init__.py         — Package init
  __main__.py         — Entry point: python3 -m dragon_voice
  server.py           — aiohttp WebSocket server + HTTP + CORS middleware + agentic wiring
  pipeline.py         — STT→LLM→TTS orchestration with VAD + dictation + post-processing
  conversation.py     — Multi-turn ConversationEngine with tool-calling + memory-augmented context
  sessions.py         — SessionManager (create/resume/pause/end lifecycle)
  messages.py         — MessageStore (append-only, LLM context builder, includes tool messages)
  db.py               — Async SQLite layer (aiosqlite, WAL mode, full CRUD)
  memory.py           — MemoryService: facts + documents + RAG with Ollama embeddings
  config.py           — Config dataclasses (incl. ToolsConfig, MemoryConfig)
  config.yaml         — Default configuration
  api/                — Modular REST API package (50 endpoints)
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
  tools/              — Tool-calling infrastructure
    __init__.py       — Exports ToolRegistry, Tool
    base.py           — Tool abstract base class
    registry.py       — ToolRegistry: register, parse XML markers, execute
    web_search.py     — DuckDuckGo web search (no API key)
    memory_tools.py   — remember + recall tools (interface to MemoryService)
    datetime_tool.py  — Current date/time tool
  stt/                — STT backends (moonshine, whisper_cpp, vosk, openrouter)
  tts/                — TTS backends (piper, kokoro, edge_tts, openrouter)
  llm/                — LLM backends (ollama, openrouter, lmstudio, npu_genie)
  notes/              — Notes module (CRUD + search + audio ingestion)
tests/                — E2E test suite
  test_api_e2e.py     — 29 tests (14 single-step, 8 multi-step, 7 complex chained)
docs/
  protocol.md         — WebSocket protocol spec (Tab5 ↔ Dragon)
  npu-setup.md        — Qualcomm NPU / QAIRT SDK setup guide
LEARNINGS.md          — Institutional knowledge (MANDATORY reading)
```

## Testing

### E2E API Tests (`tests/test_api_e2e.py`)
- **29 total tests** — all passing
  - **14 single-step tests:** Basic CRUD operations (create session, list devices, store fact, etc.)
  - **8 multi-step tests:** Sequences requiring state (create session → send messages → retrieve history, etc.)
  - **7 complex chained tests:** Full workflows across multiple subsystems (session + chat + memory + tools, etc.)

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
