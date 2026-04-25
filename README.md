# TinkerBox

**The Brain of TinkerClaw** -- Dragon Q6A server stack for the TinkerClaw voice assistant platform.

TinkerBox runs on a Radxa Dragon Q6A (Qualcomm QCS6490, ARM64) and provides the
complete AI pipeline for TinkerClaw devices: speech-to-text, language model
inference, text-to-speech synthesis, multi-turn conversation management, session
persistence, a REST API, browser streaming, and a web dashboard. The companion
[TinkerTab](https://github.com/lorcan35/TinkerTab) firmware (ESP32-P4 / M5Stack
Tab5) is a thin client -- it captures audio and displays results, but all
intelligence lives here.

---

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Backend Options](#backend-options)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [API Reference](#api-reference)
- [WebSocket Protocol](#websocket-protocol)
- [Database Schema](#database-schema)
- [Cloud Mode](#cloud-mode)
- [Deployment](#deployment)
- [Testing](#testing)
- [Project Structure](#project-structure)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [License](#license)

---

## Features

- **Voice Pipeline (STT -> LLM -> TTS)** -- End-to-end voice conversation in a
  single streaming pipeline. Audio in, audio out, with sentence-level TTS
  streaming for low perceived latency.

- **Multi-Turn Conversation** -- Full conversation context is maintained across
  turns using an append-only message store backed by SQLite. The conversation
  engine builds LLM context from session history automatically.

- **Session Management** -- Sessions survive WebSocket disconnects. When a device
  reconnects, it resumes its previous session with full message history intact.
  Stale sessions are auto-ended after a configurable timeout (default: 30 min).

- **Dictation Mode** -- A secondary input mode where the Tab5 handles VAD
  locally, sends segment markers to Dragon, and Dragon transcribes each segment
  independently. The full transcript is assembled server-side with optional
  LLM-generated title and summary.

- **Cloud Mode** -- Toggle between local (Moonshine + Piper) and cloud
  (OpenRouter) STT/TTS backends at runtime. A single WebSocket message from the
  client switches both backends without reconnecting.

- **Notes API** -- Create, list, search, update, and delete notes. Notes can be
  created from text or raw audio uploads. Semantic search support is stubbed for
  future embedding-based retrieval.

- **Hot-Swappable Backends** -- Change STT, TTS, or LLM backends at runtime via
  the REST API or dashboard. Active pipeline instances are re-initialized
  in-place without dropping the WebSocket connection.

- **Device Registry** -- Every connecting device is registered with its hardware
  ID, firmware version, platform, and capabilities. Online/offline status is
  tracked in real time.

- **Scoped Configuration** -- Config values can be set at global, device, or
  session scope. More specific scope wins. Runtime-mutable via the REST API.

- **NPU Inference** -- Llama 3.2 1B on Qualcomm Hexagon DSP (HTP) at ~8 tok/s,
  roughly 30x faster than CPU-based Ollama on the same hardware.

- **CDP Browser Streaming** -- Screencast a Chromium instance to the Tab5 via
  MJPEG, with touch events relayed back through Chrome DevTools Protocol.

- **Web Dashboard** -- 11-tab management UI with pipeline config, device list,
  connection monitoring, OTA management, and E2E debug suite (port 3500).

---

## Architecture

```
Tab5 (ESP32-P4)                         Dragon Q6A (this repo)
+--------------------+                  +--------------------------------+
| LVGL UI            |                  |                                |
| Mic / Speaker      |  WS /ws/voice   |  Voice Server (:3502)          |
| Touch / Camera     | <=============> |    +-- WebSocket protocol      |
|                    |  PCM + JSON      |    +-- STT (moonshine/whisper) |
| WiFi client        |                  |    +-- LLM (NPU/Ollama/cloud) |
|                    |  GET /stream     |    +-- TTS (piper/kokoro/edge) |
| MJPEG display      | <-------------- |    +-- ConversationEngine      |
|                    |  WS /ws/touch    |    +-- SessionManager          |
|                    | ---------------> |    +-- REST API /api/v1/       |
|                    |                  |    +-- Notes API /api/notes/   |
| mDNS discovery     |                  |                                |
|                    |                  |  CDP Server (:3501)            |
|                    |                  |    +-- MJPEG screencast        |
|                    |                  |    +-- Touch -> CDP mouse      |
|                    |                  |    +-- UDP JPEG streamer       |
|                    |                  |                                |
|                    |                  |  Dashboard (:3500)             |
|                    |                  |    +-- Web UI (status/config)  |
|                    |                  |    +-- Proxies to voice/CDP    |
|                    |                  |                                |
|                    |                  |  Chromium (:9222 CDP)          |
|                    |                  |  Ollama (:11434)               |
|                    |                  |  NPU Genie (HTP, ~8 tok/s)    |
+--------------------+                  +--------------------------------+
```

### Service Map

| Service | Port | systemd Unit | Description |
|---------|------|-------------|-------------|
| Dashboard | 3500 | `tinkerclaw-dashboard` | Web UI for status, config, device management |
| Dragon CDP | 3501 | `tinkerclaw` | MJPEG screencast + touch relay via Chrome DevTools Protocol |
| Voice + API | 3502 | `tinkerclaw-voice` | Voice pipeline (STT/LLM/TTS), sessions, REST API, Notes API |
| Telegram Bot | -- | `tinkerclaw-telegram` | Isolated Telegram chat bot using OpenRouter |
| mDNS | -- | `tinkerclaw-mdns` | Advertises `_tinkerclaw._tcp` for Tab5 auto-discovery |
| Chromium | 9222 | (launched by `tinkerclaw`) | CDP target browser for screen streaming |
| Ollama | 11434 | `ollama` | Local LLM inference (CPU fallback, ~0.24 tok/s) |
| SearXNG | 8888 | `searxng` | Self-hosted metasearch engine (web_search tool backend) |
| NPU Genie | -- | (via voice pipeline) | Llama 3.2 1B on QCS6490 Hexagon DSP (~8 tok/s) |

### Voice Pipeline Flow

```
Mic Audio (PCM int16, 16kHz)
  |
  v
[VAD] -- energy-based silence detection (configurable threshold)
  |
  v
[STT] -- Moonshine / Whisper.cpp / Vosk / OpenRouter
  |
  v
[Conversation Engine] -- builds context from message history, calls LLM
  |
  v
[LLM] -- streaming token generation (Ollama / OpenRouter / LM Studio / NPU Genie)
  |
  v
[Sentence Buffer] -- flushes at sentence boundaries for low-latency TTS
  |
  v
[TTS] -- per-sentence synthesis (Piper / Kokoro / Edge TTS / OpenRouter)
  |
  v
[Resample] -- TTS rate (e.g. 22050 Hz) -> 16kHz for Tab5
  |
  v
[Pace & Stream] -- 4096-byte chunks, paced at ~80% real-time to prevent buffer overflow
  |
  v
Speaker Audio (PCM int16, 16kHz)
```

---

## Backend Options

### Speech-to-Text (STT)

| Backend | Key | Description | Runs On |
|---------|-----|-------------|---------|
| Moonshine | `moonshine` | Lightweight ONNX-based STT, fast on ARM64 | Local (CPU) |
| Whisper.cpp | `whisper_cpp` | OpenAI Whisper via whisper.cpp bindings | Local (CPU) |
| Vosk | `vosk` | Kaldi-based offline STT | Local (CPU) |
| OpenRouter | `openrouter` | Cloud STT via OpenRouter API (gpt-audio-mini) | Cloud |

### Large Language Model (LLM)

| Backend | Key | Description | Runs On |
|---------|-----|-------------|---------|
| OpenRouter | `openrouter` | Any model via OpenRouter (Claude, GPT, Gemma, etc.) | Cloud |
| Ollama | `ollama` | Local Ollama instance (gemma3:4b, llama3.2, etc.) | Local (CPU) |
| LM Studio | `lmstudio` | LM Studio server with OpenAI-compatible API | Local / Remote |
| NPU Genie | `npu_genie` | Llama 3.2 1B on Qualcomm Hexagon DSP via QAIRT SDK | Local (NPU) |

### Text-to-Speech (TTS)

| Backend | Key | Description | Runs On |
|---------|-----|-------------|---------|
| Piper | `piper` | Fast offline neural TTS, 22050 Hz output | Local (CPU) |
| Kokoro | `kokoro` | ONNX-based TTS with multiple voices | Local (CPU) |
| Edge TTS | `edge_tts` | Microsoft Edge cloud TTS (free, high quality) | Cloud |
| OpenRouter | `openrouter` | Cloud TTS via OpenRouter API (gpt-audio-mini) | Cloud |

---

## Prerequisites

- **Python 3.12+**
- **Linux** -- ARM64 (Radxa, Raspberry Pi) or x86_64
- **Target hardware**: Radxa Dragon Q6A (Qualcomm QCS6490) recommended, but any
  Linux machine works
- **pip** for package installation
- **SQLite 3.35+** (ships with Python 3.12+, WAL mode + FTS5 support needed)
- **Chromium** (optional, for CDP browser streaming)
- **Ollama** (optional, for local LLM inference)
- **OpenRouter API key** (optional, for cloud backends)

---

## Quick Start

### On the Dragon Q6A

```bash
# 1. Clone the repo
git clone https://github.com/lorcan35/TinkerBox.git
cd TinkerBox

# 2. Run setup (installs Python packages, checks Chromium)
./setup.sh

# 3. Install voice pipeline dependencies
pip3 install --break-system-packages -r dragon_voice/requirements.txt

# 4. (Optional) Install STT/TTS backend packages you need
pip3 install --break-system-packages pywhispercpp   # for whisper_cpp
pip3 install --break-system-packages sherpa-onnx     # for moonshine
pip3 install --break-system-packages vosk             # for vosk
pip3 install --break-system-packages piper-tts        # for piper
pip3 install --break-system-packages kokoro-onnx      # for kokoro
pip3 install --break-system-packages edge-tts          # for edge_tts

# 5. Set your OpenRouter API key (if using cloud backends)
export OPENROUTER_API_KEY="sk-or-..."
# Or edit dragon_voice/config.yaml directly

# 6. Start the voice server
python3 -m dragon_voice
```

### One-Command Launch (all services)

```bash
./start.sh                    # Chromium + Dragon CDP server
python3 -m dragon_voice &     # Voice server (separate terminal)
python3 dashboard.py &        # Dashboard (separate terminal)

# Or install as systemd services for auto-start on boot:
# (see "Deployment → systemd Services" below for the install commands)
```

### Deploy from a workstation

```bash
# Sync code to the Dragon
sshpass -p 'radxa' scp -r dragon_voice/ radxa@192.168.1.91:/home/radxa/
sshpass -p 'radxa' scp dashboard.py dragon_server.py schema.sql radxa@192.168.1.91:/home/radxa/

# Restart the voice service
sshpass -p 'radxa' ssh radxa@192.168.1.91 \
  "echo 'radxa' | sudo -S systemctl restart tinkerclaw-voice"
```

---

## Configuration

Configuration is loaded from `dragon_voice/config.yaml` at startup. Values can
be overridden via environment variables or CLI flags, and hot-reloaded at runtime
through the REST API.

### Config File (`dragon_voice/config.yaml`)

```yaml
server:
  host: "0.0.0.0"
  port: 3502

stt:
  backend: "moonshine"          # moonshine, whisper_cpp, vosk, openrouter
  model: "medium"
  language: "en"
  moonshine_model_path: ""      # custom model path (empty = auto-download)
  whisper_model_path: ""
  vosk_model_path: ""

tts:
  backend: "piper"              # piper, kokoro, edge_tts, openrouter
  piper_model: "en_US-lessac-medium"
  piper_data_dir: ""
  kokoro_model_path: ""
  kokoro_voice: "af_heart"
  edge_voice: "en-US-AriaNeural"
  sample_rate: 22050

llm:
  backend: "openrouter"         # ollama, openrouter, lmstudio, npu_genie
  ollama_url: "http://localhost:11434"
  ollama_model: "ministral-3:3b"  # see CLAUDE.md "Local LLM Benchmarks" for model choice
  openrouter_api_key: ""        # reads from OPENROUTER_API_KEY env var if empty
  openrouter_model: "anthropic/claude-3.5-haiku"
  openrouter_url: "https://openrouter.ai/api/v1"
  lmstudio_url: "http://localhost:1234/v1"
  lmstudio_model: "default"
  system_prompt: "You are Tinker, a helpful AI assistant. Reply in 1-2 sentences maximum."
  max_tokens: 256
  temperature: 0.7

audio:
  input_sample_rate: 16000      # Tab5 sends 16kHz PCM
  input_channels: 1
  output_sample_rate: 22050     # TTS native rate (resampled to 16kHz before sending)
  vad_enabled: true
  vad_silence_ms: 600           # ms of silence before triggering STT
```

### Environment Variable Overrides

Any config value can be overridden with an environment variable following the
pattern `DRAGON_VOICE_{SECTION}_{KEY}`:

```bash
export DRAGON_VOICE_STT_BACKEND=moonshine
export DRAGON_VOICE_LLM_BACKEND=npu_genie
export DRAGON_VOICE_TTS_BACKEND=piper
export DRAGON_VOICE_LLM_OPENROUTER_API_KEY=sk-or-...
export DRAGON_VOICE_AUDIO_VAD_SILENCE_MS=800
export DRAGON_VOICE_LLM_MAX_TOKENS=512
```

Type coercion is automatic (booleans, integers, floats are detected from the
existing config value type).

You can also point to a different config file:

```bash
export DRAGON_VOICE_CONFIG=/path/to/custom/config.yaml
```

### CLI Flags

```bash
python3 -m dragon_voice \
  --config /path/to/config.yaml \
  --port 3503 \
  --host 0.0.0.0 \
  --stt moonshine \
  --tts piper \
  --llm openrouter \
  --log-level DEBUG
```

CLI flags take the highest priority and override both the config file and
environment variables.

### Hot-Reload via REST API

```bash
# Swap to a different LLM backend at runtime
curl -X POST http://dragon:3502/api/config \
  -H "Content-Type: application/json" \
  -d '{"llm": {"backend": "ollama", "ollama_model": "ministral-3:3b"}}'

# Response includes the number of active pipelines that were reloaded
# {"status": "ok", "message": "Config updated, 1 pipelines reloaded", ...}
```

---

## API Reference

All REST endpoints are served on port 3502 alongside the WebSocket server.

### Core HTTP Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | HTML status page with backend info and uptime |
| GET | `/health` | JSON health check |
| GET | `/api/config` | Current config (secrets redacted) |
| POST | `/api/config` | Hot-reload config, swap backends on active pipelines |

### Sessions (`/api/v1/sessions`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/sessions` | List sessions (query: `device_id`, `status`, `limit`, `offset`) |
| POST | `/api/v1/sessions` | Create session (body: `device_id`, `type`, `system_prompt`, `config`) |
| GET | `/api/v1/sessions/{id}` | Get session by ID |
| POST | `/api/v1/sessions/{id}/end` | End a session permanently |

### Messages (`/api/v1/sessions/{id}/messages`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/sessions/{id}/messages` | List messages (query: `limit`, `offset`) |
| POST | `/api/v1/sessions/{id}/chat` | Send text, stream LLM response as SSE |

The `/chat` endpoint returns a Server-Sent Events stream:

```
data: {"token": "Hello"}
data: {"token": " there!"}
data: [DONE]
```

### Devices (`/api/v1/devices`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/devices` | List devices (query: `online=true` to filter) |
| GET | `/api/v1/devices/{id}` | Get device by ID |

### Config Store (`/api/v1/config`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/config` | List config entries (query: `scope`, `scope_id`) |
| GET | `/api/v1/config/{key}` | Get config value (query: `scope`, `scope_id`, `resolve=true`) |
| PUT | `/api/v1/config/{key}` | Set config value (body: `value`, `scope`, `scope_id`) |

When `resolve=true`, the server applies scope resolution: session > device > global.

### Transcription

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/transcribe` | Upload audio, get transcript back |

Headers: `Content-Type: application/octet-stream` (raw PCM) or `audio/wav`.
Optional `X-Sample-Rate` header (default: 16000).

Response:
```json
{"text": "transcribed text", "duration_s": 5.2, "stt_ms": 1234}
```

### Notes (`/api/notes`)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/notes` | Create note from text (body: `text`, `title`) |
| GET | `/api/notes` | List notes (query: `limit`, `offset`) |
| GET | `/api/notes/{id}` | Get note by ID |
| PUT | `/api/notes/{id}` | Update note (body: `title`, `transcript`, `summary`, `tags`) |
| DELETE | `/api/notes/{id}` | Delete note |
| POST | `/api/notes/search` | Semantic search (body: `query`, `limit`) |
| POST | `/api/notes/from-audio` | Create note from raw PCM audio upload |

---

## WebSocket Protocol

Full specification: [docs/protocol.md](docs/protocol.md)

**Endpoint:** `ws://<dragon-ip>:3502/ws/voice`

### Connection Lifecycle

```
Tab5                                    Dragon
  |                                       |
  |--- WS CONNECT /ws/voice ----------->|
  |--- register (JSON) ---------------->|  Device registration
  |<-- session_start (JSON) ------------|  Session assigned
  |                                       |
  |  ... voice / text conversation ...    |
  |                                       |
  |--- WS CLOSE ----------------------->|  Session -> PAUSED
  |                                       |
  |--- WS CONNECT /ws/voice ----------->|  Reconnect
  |--- register (JSON, session_id) ---->|  Resume request
  |<-- session_start (JSON) ------------|  Same session, history intact
```

### Client -> Server Messages

| Type | Frame | Description |
|------|-------|-------------|
| `register` | JSON | Device registration. **Must be the first message.** Fields: `device_id` (required), `hardware_id`, `name`, `firmware_ver`, `platform`, `capabilities`, `session_id` (for resume). |
| `start` | JSON | Begin a new voice turn. Clears the audio buffer. Optional `mode` field: `"ask"` (default, full pipeline) or `"dictate"` (STT only). |
| `stop` | JSON | End of speech. In ask mode, triggers STT -> LLM -> TTS. In dictate mode, finalizes the transcript. |
| `segment` | JSON | Dictation segment marker. Tab5 sends this when it detects a VAD pause in dictation mode. Dragon transcribes the buffered audio. |
| `cancel` | JSON | Abort current pipeline processing. |
| `text` | JSON | Text input (bypasses STT). Field: `content`. |
| `clear` | JSON | Clear conversation history. Ends current session and creates a fresh one. |
| `config_update` | JSON | Request cloud mode toggle. Field: `cloud_mode` (bool). |
| `config_ack` | JSON | Acknowledge a config push from Dragon. |
| `ping` | JSON | Application-level heartbeat. Dragon responds with `pong`. |
| (binary) | Binary | Raw PCM audio: int16, 16kHz, mono. |

### Server -> Client Messages

| Type | Frame | Description |
|------|-------|-------------|
| `session_start` | JSON | Session assignment after registration. Fields: `session_id`, `device_id`, `resumed`, `message_count`, `config`. |
| `stt` | JSON | Transcription result. Fields: `text`, `stt_ms`. |
| `stt_partial` | JSON | Partial dictation segment transcript. Fields: `text`, `stt_ms`. |
| `llm` | JSON | Streaming LLM token. Field: `text`. |
| `llm_done` | JSON | LLM generation complete. Field: `llm_ms`. |
| `tts_start` | JSON | TTS audio stream is beginning. |
| `tts_end` | JSON | TTS audio stream is complete. Field: `tts_ms`. |
| `dictation_summary` | JSON | LLM-generated title and summary for a completed dictation. Fields: `title`, `summary`. |
| `note_created` | JSON | Note created from a recording. |
| `config_update` | JSON | Config push from Dragon to Tab5. |
| `error` | JSON | Error message. Fields: `code`, `message`. Error codes: `stt_failed`, `llm_failed`, `tts_failed`, `session_invalid`, `rate_limited`, `internal`. |
| `event` | JSON | Generic system event. Fields: `event`, `data`. |
| `pong` | JSON | Response to application-level ping. |
| (binary) | Binary | TTS audio: PCM int16, 16kHz, mono (resampled from TTS engine rate). Chunked at 4096 bytes, paced at ~80% real-time. |

### Voice Input Flow (Ask Mode)

```
Client: {"type": "start"}
Client: [binary PCM frames...]
Client: {"type": "stop"}
Server: {"type": "stt", "text": "What is the weather?", "stt_ms": 340}
Server: {"type": "llm", "text": "It's"}
Server: {"type": "llm", "text": " sunny"}
Server: {"type": "llm", "text": " today."}
Server: {"type": "tts_start"}
Server: [binary TTS audio frames...]
Server: {"type": "tts_end", "tts_ms": 280}
Server: {"type": "llm_done", "llm_ms": 1200}
```

### Audio Format

| Parameter | Value |
|-----------|-------|
| Encoding | PCM signed 16-bit little-endian (int16) |
| Sample rate (input) | 16000 Hz |
| Sample rate (output) | 16000 Hz (Dragon resamples from TTS engine rate) |
| Channels | 1 (mono) |

---

## Database Schema

SQLite database with WAL mode and foreign keys enabled. Six tables managed by
`dragon_voice/db.py` via aiosqlite. Schema defined in `schema.sql`.

### Tables

#### `devices`

Registered client devices. Tracks hardware identity, capabilities, and online status.

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PK | UUID from device NVS or server-assigned |
| `hardware_id` | TEXT UNIQUE | MAC address or serial (immutable hardware identity) |
| `name` | TEXT | User-assigned friendly name |
| `firmware_ver` | TEXT | e.g. "0.4.2" |
| `platform` | TEXT | e.g. "esp32p4-tab5", "web" |
| `capabilities` | TEXT (JSON) | `{mic: true, speaker: true, screen: true, ...}` |
| `config` | TEXT (JSON) | Per-device config overrides |
| `is_online` | INTEGER | 1 if currently connected |
| `last_seen_at` | REAL | Unix timestamp |
| `created_at` | REAL | Unix timestamp |
| `updated_at` | REAL | Unix timestamp |

#### `sessions`

Conversation sessions. Survive disconnects (`active` -> `paused` -> `active` -> `ended`).

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PK | Short hex UUID (12 chars) |
| `device_id` | TEXT FK | NULL for API-only sessions |
| `type` | TEXT | `conversation`, `recording`, or `skill` |
| `status` | TEXT | `active`, `paused`, or `ended` |
| `title` | TEXT | Auto-generated or user-set |
| `system_prompt` | TEXT | Session-level prompt override |
| `config` | TEXT (JSON) | Session-level config (model, temperature, etc.) |
| `metadata` | TEXT (JSON) | Arbitrary session data (skill context, tags) |
| `message_count` | INTEGER | Denormalized for fast listing |
| `created_at` | REAL | Unix timestamp |
| `last_active_at` | REAL | Unix timestamp |
| `ended_at` | REAL | NULL until ended |

#### `messages`

Append-only conversation messages. Never mutated after creation.

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PK | Short UUID |
| `session_id` | TEXT FK | Parent session |
| `role` | TEXT | `user`, `assistant`, `system`, or `tool` |
| `content` | TEXT | Message text content |
| `input_mode` | TEXT | `voice`, `text`, or `system` |
| `interrupted` | INTEGER | 1 if the user interrupted the assistant |
| `audio_duration_s` | REAL | Duration of voice input (NULL for text) |
| `token_count` | INTEGER | LLM tokens used (NULL for user messages) |
| `model` | TEXT | Which LLM model generated this (NULL for user) |
| `latency_ms` | REAL | End-to-end processing time |
| `created_at` | REAL | Unix timestamp |

#### `notes`

Enriched session artifacts. Can be linked to a session or standalone.

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PK | Short UUID |
| `session_id` | TEXT FK | NULL for standalone notes |
| `title` | TEXT | Title (auto-generated or user-set) |
| `transcript` | TEXT | Raw STT output or user text |
| `summary` | TEXT | LLM-generated summary |
| `tags` | TEXT (JSON) | Array of tag strings |
| `source` | TEXT | `audio`, `text`, or `import` |
| `duration_s` | REAL | Audio duration if from recording |
| `word_count` | INTEGER | Word count of transcript |
| `embedding` | BLOB | JSON-encoded float array for semantic search |
| `created_at` | REAL | Unix timestamp |
| `updated_at` | REAL | Unix timestamp |

#### `events`

System event log for audit trail, debugging, and real-time dashboard updates.

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment |
| `type` | TEXT | e.g. `session.created`, `device.connected`, `message.added` |
| `session_id` | TEXT | Context session (NULL for system events) |
| `device_id` | TEXT | Context device (NULL for API events) |
| `data` | TEXT (JSON) | Event payload |
| `created_at` | REAL | Unix timestamp |

#### `config`

Key-value config store with scope resolution: global -> device -> session.

| Column | Type | Description |
|--------|------|-------------|
| `key` | TEXT | e.g. `llm.backend`, `tts.voice` |
| `value` | TEXT | JSON-encoded value |
| `scope` | TEXT | `global`, `device`, or `session` |
| `scope_id` | TEXT | device_id or session_id (NULL for global) |
| `updated_at` | REAL | Unix timestamp |

Primary key: `(key, scope, scope_id)`.

---

## Cloud Mode

Cloud mode replaces local STT and TTS backends with OpenRouter-hosted
alternatives, useful when you want higher quality transcription/synthesis or when
running on hardware without enough resources for local models.

### How It Works

1. The Tab5 (or any WebSocket client) sends:
   ```json
   {"type": "config_update", "cloud_mode": true}
   ```

2. Dragon switches the STT backend to `openrouter` and the TTS backend to
   `openrouter`, automatically propagating the LLM's OpenRouter API key to
   both.

3. Active pipeline instances are hot-swapped in place -- no reconnection needed.

4. Dragon confirms the change back to the client:
   ```json
   {"type": "config_update", "config": {"stt": "openrouter", "tts": "openrouter", "llm": "openrouter", "cloud_mode": true}}
   ```

### Setup

Set your OpenRouter API key in one of these ways:

```bash
# Environment variable (recommended)
export OPENROUTER_API_KEY="sk-or-v1-..."

# Or in config.yaml
llm:
  openrouter_api_key: "sk-or-v1-..."
```

The key is automatically propagated to STT and TTS backends when cloud mode is
activated. The OpenRouter STT/TTS backends use the `gpt-audio-mini` model for
audio input/output capabilities.

### Switching Back to Local

Send `cloud_mode: false` to revert:
```json
{"type": "config_update", "cloud_mode": false}
```

This switches STT back to `moonshine` and TTS back to `piper`.

---

## Deployment

### systemd Services

The canonical unit files live in [`systemd/`](systemd/) and use the
`tinkerclaw-*` naming convention.  Install them by copying the files in
that directory to `/etc/systemd/system/` (or run the matching install
helper if your branch ships one):

```bash
sudo install -m 644 systemd/tinkerclaw-voice.service        /etc/systemd/system/
sudo install -m 644 systemd/tinkerclaw-gateway.service      /etc/systemd/system/
sudo install -m 644 systemd/tinkerclaw-ngrok.service        /etc/systemd/system/
sudo install -m 644 systemd/tinkerclaw-backup.service       /etc/systemd/system/
sudo install -m 644 systemd/tinkerclaw-backup.timer         /etc/systemd/system/
# Plus any drop-ins from systemd/*.service.d/
sudo systemctl daemon-reload
```

The active units on a deployed Dragon (verified 2026-04-25):

- `tinkerclaw` -- Dragon CDP streaming server (port 3501) + Chromium child process
- `tinkerclaw-voice` -- Voice pipeline (STT/LLM/TTS, port 3502, REST API, Notes API)
- `tinkerclaw-dashboard` -- Web dashboard (port 3500)
- `tinkerclaw-mdns` -- mDNS advertisement (`_tinkerclaw._tcp`)
- `tinkerclaw-gateway` -- Optional TinkerClaw agent runner (port 18789, localhost only)
- `tinkerclaw-ngrok` -- ngrok tunnels (Dashboard + Voice + Gateway)
- `tinkerclaw-backup.timer` -- Hourly snapshot timer (DB + config)

> **Note:** The legacy `install-services.sh` script in the repo root
> generates units named `tinkerbox-*`, which **do not match** the
> canonical names in `systemd/` or what's actually deployed.  Prefer
> the explicit `install` calls above; the script is kept around for
> historical reference and will be retired or rewritten in a future
> change (tracked in #86 follow-up).

Manage the services:

```bash
# Start the voice service
sudo systemctl start tinkerclaw-voice

# Check status
sudo systemctl status tinkerclaw-voice

# View logs
journalctl -u tinkerclaw-voice -f

# Restart after code changes
sudo systemctl restart tinkerclaw-voice
```

The optional Telegram bot runs as a separate unit:

```bash
cp telegram.env.example telegram.env   # fill in token + OpenRouter key
sudo install -m 644 systemd/tinkerclaw-telegram.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tinkerclaw-telegram.service
```

### SSH to Dragon

```bash
# Default credentials
ssh radxa@192.168.1.91   # password: radxa

# Or with sshpass for scripting
sshpass -p 'radxa' ssh radxa@192.168.1.91
```

### Deploy Script (from workstation)

```bash
# Sync voice pipeline code
sshpass -p 'radxa' scp -r dragon_voice/ radxa@192.168.1.91:/home/radxa/

# Sync top-level files
sshpass -p 'radxa' scp dashboard.py dragon_server.py schema.sql \
  radxa@192.168.1.91:/home/radxa/

# Restart the voice service
sshpass -p 'radxa' ssh radxa@192.168.1.91 \
  "echo 'radxa' | sudo -S systemctl restart tinkerclaw-voice"
```

### Python Package Notes

On the Dragon (Debian/Radxa OS), use `--break-system-packages` for pip installs
due to PEP 668:

```bash
pip3 install --break-system-packages -r dragon_voice/requirements.txt
```

The voice server runs as a Python module:

```bash
PYTHONPATH=/home/radxa python3 -m dragon_voice
```

---

## Testing

Tests live in the `tests/` directory and are designed to run against a live
Dragon instance or locally with mocked backends.

### Unit / Integration Tests

```bash
# Run foundation tests (Database, SessionManager, MessageStore)
pytest tests/test_foundation.py -v

# Run all tests
pytest tests/ -v
```

### Live Tests (run on Dragon)

These tests connect to the running voice server and exercise the full stack:

```bash
# Multi-turn conversation test
pytest tests/test_multiturn_live.py -v

# Session resume test (disconnect + reconnect)
pytest tests/test_resume_live.py -v

# Full end-to-end suite
python3 tests/e2e_full_suite.py

# Audit wave 6 WS-level regressions (D5 tool-XML strip + D6 media order)
python3 tests/audit/test_d5_d6_ws.py
```

The audit probe connects directly to `/ws/voice`, bypasses Tab5, and asserts
two contract invariants: (1) the `llm` token stream never leaks
`<tool>...</tool>` markup to the client, (2) on code-block responses the
`text_update` event arrives BEFORE the `media` event so clients can
deterministically replace the raw-markdown bubble. See
`tests/audit/test_d5_d6_ws.py` for the full contract.

### Test Dependencies

```bash
pip3 install --break-system-packages pytest pytest-asyncio
```

---

## Project Structure

```
TinkerBox/
|-- schema.sql                     Database schema (6 tables)
|-- dragon_server.py               CDP streaming + touch WebSocket (port 3501)
|-- dashboard.py                   Web dashboard aggregator (port 3500)
|-- udp_streamer.py                UDP JPEG streaming for low-latency display
|-- telegram_bot.py                Standalone Telegram polling bot (OpenRouter)
|
|-- dragon_voice/                  Voice pipeline package (port 3502)
|   |-- __init__.py                Package init
|   |-- __main__.py                Entry point: python3 -m dragon_voice
|   |-- server.py                  aiohttp WebSocket server + HTTP endpoints
|   |-- pipeline.py                STT -> LLM -> TTS orchestration with VAD
|   |-- conversation.py            Multi-turn ConversationEngine (DB-backed)
|   |-- sessions.py                SessionManager (create/resume/pause/end)
|   |-- messages.py                MessageStore (append-only, context builder)
|   |-- db.py                      Async SQLite layer (aiosqlite, WAL mode)
|   |-- api.py                     REST API v1 routes (/api/v1/*)
|   |-- config.py                  Config dataclasses + YAML/env loading
|   |-- config.yaml                Default configuration
|   |-- requirements.txt           Voice pipeline Python dependencies
|   |
|   |-- stt/                       Speech-to-Text backends
|   |   |-- base.py                STTBackend abstract base class
|   |   |-- moonshine_stt.py       Moonshine ONNX backend
|   |   |-- whisper_cpp.py         Whisper.cpp backend
|   |   |-- vosk_stt.py            Vosk backend
|   |   |-- openrouter_stt.py      OpenRouter cloud STT backend
|   |
|   |-- tts/                       Text-to-Speech backends
|   |   |-- base.py                TTSBackend abstract base class
|   |   |-- piper_tts.py           Piper neural TTS backend
|   |   |-- kokoro_tts.py          Kokoro ONNX backend
|   |   |-- edge_tts_backend.py    Microsoft Edge TTS backend
|   |   |-- openrouter_tts.py      OpenRouter cloud TTS backend
|   |
|   |-- llm/                       Large Language Model backends
|   |   |-- base.py                LLMBackend abstract base class
|   |   |-- ollama_llm.py          Ollama local inference backend
|   |   |-- openrouter_llm.py      OpenRouter cloud backend
|   |   |-- lmstudio_llm.py        LM Studio backend
|   |   |-- npu_genie.py           Qualcomm NPU Genie backend (HTP)
|   |
|   |-- notes/                     Notes module
|       |-- db.py                  Notes database layer
|       |-- service.py             Notes business logic
|       |-- api.py                 Notes REST API routes (/api/notes/*)
|
|-- tests/                         Test suite
|   |-- test_foundation.py         Foundation module unit tests
|   |-- test_multiturn_live.py     Live multi-turn conversation tests
|   |-- test_resume_live.py        Live session resume tests
|   |-- test_e2e_dragon.py         End-to-end Dragon tests
|   |-- e2e_full_suite.py          Full E2E test runner
|
|-- docs/
|   |-- protocol.md                WebSocket protocol specification
|   |-- npu-setup.md               Qualcomm NPU / QAIRT SDK setup guide
|   |-- telegram-bot.md            Telegram bot deployment notes
|
|-- systemd/
|   |-- tinkerclaw-telegram.service  Telegram bot systemd unit
|
|-- requirements.txt               Top-level Python dependencies
|-- secrets.yaml.example           Example secrets file (API keys)
|-- telegram.env.example           Example Telegram bot environment
|-- setup.sh                       Dependency installer
|-- start.sh                       One-command launcher (Chromium + Dragon)
|-- systemd/                       systemd unit files (canonical install path)
|-- launch-chromium.sh             Chromium CDP launcher
|-- start-chat.sh                  Chat launcher script
|-- CLAUDE.md                      Developer guide, sprint status, architecture
|-- LEARNINGS.md                   Institutional knowledge (bugs, fixes, gotchas)
```

---

## Troubleshooting

### OpenRouter API Key Not Set

**Symptom:** Server starts but LLM calls fail with authentication errors.

**Fix:** Set the API key via environment variable or config:
```bash
export OPENROUTER_API_KEY="sk-or-v1-..."
# Or set llm.openrouter_api_key in config.yaml
```

### STT Model Fails to Load

**Symptom:** `ImportError` or `FileNotFoundError` on startup.

**Fix:** Install the required backend package:
```bash
pip3 install --break-system-packages sherpa-onnx    # moonshine
pip3 install --break-system-packages pywhispercpp   # whisper_cpp
pip3 install --break-system-packages vosk            # vosk
```

For Moonshine, the model is auto-downloaded on first use. Ensure the cache
directory is writable. Check the moonshine-voice API version (v0.0.51+ changed
the download API).

### VAD Not Triggering / Triggering Too Early

**Symptom:** The pipeline never processes audio, or processes too soon.

**Fix:** Adjust the VAD silence threshold in config:
```yaml
audio:
  vad_silence_ms: 800    # increase for slower speakers (default: 600)
```

Or disable server-side VAD entirely (Tab5 sends explicit `start`/`stop`):
```yaml
audio:
  vad_enabled: false
```

### TTS Audio Sounds Choppy on Tab5

**Symptom:** Playback stutters or has gaps.

**Fix:** This is typically a buffer overflow on the Tab5 side. The server paces
audio chunks at ~80% real-time, but network jitter can cause issues. Ensure the
Tab5 ring buffer is large enough, and check that the sample rate matches (16kHz
PCM int16 mono).

### Ollama is Very Slow (~0.24 tok/s)

**Expected on ARM64 CPU.** Ollama runs on CPU only on the QCS6490. Use the NPU
Genie backend instead for ~8 tok/s, or use OpenRouter for cloud inference.

### NPU Genie Backend Fails

**Symptom:** `npu_genie` backend fails to initialize.

**Fix:** Ensure the QAIRT SDK is installed and the model files are in place. See
[docs/npu-setup.md](docs/npu-setup.md) for the full setup guide. Check that
`genie_model_dir` and `genie_config` point to valid paths in the config.

### Session Not Resuming on Reconnect

**Symptom:** Device gets a new session every time it reconnects.

**Fix:** Ensure the Tab5 stores the `session_id` from `session_start` in NVS and
sends it back in the `register` message on reconnect. Sessions auto-end after 30
minutes of inactivity (configurable via `SessionManager` timeout).

### Port Already in Use

**Symptom:** `OSError: [Errno 98] Address already in use`

**Fix:**
```bash
# Find and kill the process using port 3502
lsof -i :3502
kill <PID>

# Or use a different port
python3 -m dragon_voice --port 3503
```

---

## Contributing

1. Check [LEARNINGS.md](LEARNINGS.md) first -- your bug might already be documented.
2. Create a GitHub issue before starting work.
3. Branch from `main` (e.g., `feat/my-feature` or `fix/my-bug`).
4. Every commit must reference an issue (`refs #N` or `closes #N`).
5. Add any new bugs, fixes, or gotchas to LEARNINGS.md.
6. Push and merge to main.

### Adding a New Backend

All backends follow the same pattern:

1. Implement the abstract base class (`STTBackend`, `TTSBackend`, or `LLMBackend`).
2. Add the backend to the factory registry in the corresponding `__init__.py`.
3. Add any new config fields to the relevant dataclass in `config.py`.
4. Update `config.yaml` with the new backend's options.

---

## Companion Projects

- **[TinkerTab](https://github.com/lorcan35/TinkerTab)** -- ESP32-P4 firmware
  for the M5Stack Tab5 (thin client). Handles LVGL UI, mic/speaker, camera,
  touch, SD card, WiFi, and NVS settings.

---

## License

MIT
