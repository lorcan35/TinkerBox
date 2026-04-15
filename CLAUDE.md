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
- **Password:** `<DRAGON_SSH_PASSWORD>`
- **SSH:** `ssh radxa@192.168.1.91  # password in ~/.ssh/config or use key auth`
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
Tab5 sends `{"type":"config_update","voice_mode":0|1|2,"llm_model":"...","conn_mode":0|1|2}`. Dragon hot-swaps backends:

**Connection mode (`conn_mode`):** Tab5 sends `conn_mode` in config_update to indicate its network path:
- `0` = LAN direct (low latency, no proxy)
- `1` = ngrok tunnel (higher latency, WS keepalive required)
- `2` = mixed / unknown

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
| #22 | Dashboard conversation viewer | DONE (11-tab SPA: Overview, Conversations, Chat, Devices, Notes, Logs, Memory, Documents, Tools, OTA, Debug) |
| — | Agentic pipeline (tool-calling) | DONE (ToolRegistry, XML parsing, web_search, remember, recall, datetime) |
| — | Memory + RAG | DONE (MemoryService, facts CRUD, document ingestion, semantic search) |
| — | E2E test suite | DONE (55 tests via Debug tab + 29 API tests) |
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

## API-First Architecture (44 REST endpoints + 1 WebSocket)

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
- **Built-in tools:** `web_search` (SearXNG, self-hosted on port 8888, returns up to 44 results), `remember` (store fact), `recall` (search memory), `datetime`, plus additional tools (10 total)
- **Compact tool format:** For local models with limited context, tool definitions are sent in a compact XML format to minimize token usage
- **Memory-augmented context:** Before every LLM call, relevant facts + document chunks injected into system prompt
- **WebSocket events:** `tool_call` and `tool_result` events sent to connected clients during tool execution
- **Max 3 tool calls per turn** to prevent infinite loops

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
  llm/                — LLM backends (ollama, openrouter, lmstudio, npu_genie, tinkerclaw)
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
- **29 API tests** — all passing
  - **14 single-step tests:** Basic CRUD operations (create session, list devices, store fact, etc.)
  - **8 multi-step tests:** Sequences requiring state (create session → send messages → retrieve history, etc.)
  - **7 complex chained tests:** Full workflows across multiple subsystems (session + chat + memory + tools, etc.)

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
