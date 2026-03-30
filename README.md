# TinkerBox

Dragon-side server stack for the **TinkerClaw** AI device. This is the brain.

TinkerBox runs on the Dragon Q6A (Radxa, Qualcomm QCS6490, ARM64) and provides
the full AI pipeline: voice conversation, session management, device registry,
REST API, browser streaming, and a web dashboard. The companion Tab5 (ESP32-P4)
is a thin client — it captures audio/touch and displays results, but all
intelligence lives here.

## Architecture

```
Tab5 (ESP32-P4)                         Dragon Q6A (this repo)
┌──────────────────┐                    ┌──────────────────────────────┐
│ LVGL UI          │                    │                              │
│ Mic / Speaker    │  WS /ws/voice      │  Voice Server (:3502)        │
│ Touch / Camera   │ ◄═══════════════► │    ├─ WebSocket protocol      │
│                  │  PCM audio + JSON   │    ├─ STT (moonshine/whisper)│
│ WiFi client      │                    │    ├─ LLM (NPU Genie/Ollama) │
│                  │  GET /stream        │    ├─ TTS (piper/kokoro/edge)│
│ MJPEG display    │ ◄──────────────── │    ├─ ConversationEngine      │
│                  │  WS /ws/touch       │    ├─ SessionManager         │
│                  │ ─────────────────► │    └─ REST API /api/v1/       │
│                  │                    │                              │
│ mDNS discovery   │  GET /health        │  CDP Server (:3501)          │
│                  │ ◄──────────────── │    ├─ MJPEG screencast        │
│                  │                    │    ├─ Touch → CDP mouse       │
│                  │                    │    └─ UDP JPEG streamer       │
│                  │                    │                              │
│                  │                    │  Dashboard (:3500)            │
│                  │                    │    ├─ Web UI (status/config)  │
│                  │                    │    └─ Proxies to voice/CDP    │
│                  │                    │                              │
│                  │                    │  Chromium (:9222 CDP)         │
│                  │                    │  Ollama (:11434)              │
│                  │                    │  NPU Genie (HTP, ~8 tok/s)   │
└──────────────────┘                    └──────────────────────────────┘
```

## Service Map

| Service | Port | SystemD Unit | Description |
|---------|------|-------------|-------------|
| Dashboard | 3500 | tinkerclaw-dashboard | Web UI for status, config, device management |
| Dragon CDP | 3501 | tinkerclaw | MJPEG screencast + touch relay via Chrome DevTools Protocol |
| Voice + API | 3502 | tinkerclaw-voice | Voice pipeline (STT/LLM/TTS), sessions, REST API |
| mDNS | -- | tinkerclaw-mdns | Advertises `_tinkerclaw._tcp` for Tab5 discovery |
| Chromium | 9222 | (launched by tinkerclaw) | CDP target browser |
| Ollama | 11434 | ollama | Local LLM inference (CPU fallback, ~0.24 tok/s) |
| NPU Genie | -- | (via voice pipeline) | Llama 3.2 1B on QCS6490 Hexagon DSP (~8 tok/s) |

## Current Features

- **Multi-turn voice conversation** -- STT -> LLM -> TTS with persistent context across turns
- **Session management** -- sessions survive WebSocket disconnects; devices resume on reconnect
- **Device registry** -- devices register with capabilities, tracked online/offline
- **Conversation engine** -- input-agnostic (voice or text), stores all messages in SQLite
- **REST API** (`/api/v1/`) -- CRUD for sessions, messages, devices, scoped config
- **NPU inference** -- Llama 3.2 1B on Qualcomm HTP at ~8 tok/s (30x faster than CPU)
- **Hot-swap backends** -- change STT/TTS/LLM at runtime via dashboard or API
- **CDP browser streaming** -- screencast Chromium to Tab5 via MJPEG + touch forwarding
- **Web dashboard** -- aggregated status, pipeline config, device list

## Quick Start

```bash
# 1. Clone to Dragon
ssh radxa@192.168.1.89   # password: radxa
git clone https://github.com/lorcan35/TinkerBox.git
cd TinkerBox

# 2. Install dependencies
pip3 install --break-system-packages -r requirements.txt

# 3. Start everything
./start.sh

# Or install as systemd services for auto-start on boot
sudo ./install-services.sh
```

### Deploy from workstation

```bash
# Sync code to Dragon
sshpass -p 'radxa' scp -r dragon_voice/ radxa@192.168.1.89:/home/radxa/
sshpass -p 'radxa' scp dashboard.py dragon_server.py schema.sql radxa@192.168.1.89:/home/radxa/

# Restart voice service
sshpass -p 'radxa' ssh radxa@192.168.1.89 "echo 'radxa' | sudo -S systemctl restart tinkerclaw-voice"
```

## File Structure

```
schema.sql                  -- Database schema (6 tables: devices, sessions, messages, notes, events, config)
dragon_server.py            -- CDP streaming + touch WebSocket (port 3501)
dashboard.py                -- Web dashboard aggregator (port 3500)
udp_streamer.py             -- UDP JPEG streaming for low-latency display
dragon_voice/               -- Voice pipeline package (port 3502)
  __init__.py               -- Package init
  __main__.py               -- Entry point: python3 -m dragon_voice
  server.py                 -- aiohttp WebSocket server + HTTP endpoints
  pipeline.py               -- STT -> LLM -> TTS orchestration with VAD
  conversation.py           -- Multi-turn ConversationEngine (DB-backed context)
  sessions.py               -- SessionManager (create/resume/pause/end lifecycle)
  messages.py               -- MessageStore (append-only, LLM context builder)
  db.py                     -- Async SQLite layer (aiosqlite, WAL mode)
  api.py                    -- REST API v1 routes (/api/v1/*)
  config.py                 -- Config dataclasses with YAML + env var loading
  config.yaml               -- Default configuration
  stt/                      -- STT backends (moonshine, whisper_cpp, vosk)
  tts/                      -- TTS backends (piper, kokoro, edge_tts)
  llm/                      -- LLM backends (ollama, openrouter, lmstudio, npu_genie)
  notes/                    -- Notes module (db, service, api) -- future
tests/                      -- E2E tests (run on Dragon)
  test_foundation.py        -- Foundation module tests
  test_multiturn_live.py    -- Multi-turn conversation tests
  test_resume_live.py       -- Session resume tests
docs/
  protocol.md               -- WebSocket protocol spec (Tab5 <-> Dragon)
  npu-setup.md              -- Qualcomm NPU / QAIRT SDK setup guide
CLAUDE.md                   -- Dev guide, sprint status, architecture decisions
LEARNINGS.md                -- Institutional knowledge (MANDATORY reading)
install-services.sh         -- Install systemd services
launch-chromium.sh          -- Start Chromium with CDP
setup.sh                    -- Install dependencies
start.sh                    -- One-command launch
```

## WebSocket Protocol

See [docs/protocol.md](docs/protocol.md) for the full spec. Summary:

- **Endpoint:** `ws://<dragon-ip>:3502/ws/voice`
- **Tab5 -> Dragon:** JSON commands (`register`, `start`, `stop`, `cancel`, `text`) + binary PCM audio (int16, 16kHz, mono)
- **Dragon -> Tab5:** JSON events (`session_start`, `stt`, `llm`, `tts_start`, `tts_end`, `error`) + binary TTS audio (PCM int16, 16kHz)

## Configuration

Config is loaded from `dragon_voice/config.yaml` with environment variable overrides:

```bash
# Override any setting via DRAGON_VOICE_SECTION_KEY
export DRAGON_VOICE_LLM_BACKEND=npu_genie
export DRAGON_VOICE_STT_BACKEND=moonshine
export DRAGON_VOICE_TTS_BACKEND=piper
```

Or hot-swap at runtime via the dashboard (port 3500) or POST to `/api/config`.

## Companion Project

- **[TinkerTab](https://github.com/lorcan35/TinkerTab)** -- ESP32-P4 firmware for the M5Stack Tab5 (thin client)

## License

MIT
