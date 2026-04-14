# TinkerClaw Sidecar — Design Spec

**Date:** 2026-04-14
**Status:** Draft
**Repos affected:** TinkerClaw (new), TinkerBox (Dragon), TinkerTab (Tab5)

---

## Summary

Fork OpenClaw as **TinkerClaw** — a full agent gateway running as an optional sidecar alongside Dragon's voice server on the Q6A board. When active (voice mode 3), Dragon acts as a dumb audio/text pipe: STT in, TTS out. TinkerClaw owns the conversation, skills, memory, and model selection.

All existing OpenClaw capabilities are preserved: channels (Telegram, WhatsApp, Discord, etc.), 50+ skills, browser automation, plugin system, multi-provider LLM support. Future ESP32 devices can register as TinkerClaw channels.

---

## Architecture

```
Tab5  ─── voice WS ───▶  Dragon Voice Server (port 3502)
                              │
                              ├── STT (Moonshine / OpenRouter)
                              ├── TTS (Piper / OpenRouter)
                              │
                              └── voice_mode:
                                   ├── 0 Local:      Dragon ConvEngine → Ollama
                                   ├── 1 Hybrid:     Dragon ConvEngine → Ollama
                                   ├── 2 Cloud:      Dragon ConvEngine → OpenRouter
                                   └── 3 TinkerClaw: tinkerclaw_llm.py → TinkerClaw Gateway
                                                                              │
                                                                      localhost:18789
                                                                              │
                                                                      ┌───────┴────────┐
                                                                      │   TinkerClaw    │
                                                                      │   Gateway       │
                                                                      │   ────────────  │
                                                                      │   Agent Runner  │
                                                                      │   50+ Skills    │
                                                                      │   Memory/RAG    │
                                                                      │   Ollama LLM    │
                                                                      │   OpenRouter    │
                                                                      │   Browser Auto  │
                                                                      │   All Channels  │
                                                                      └────────────────┘
```

### Input paths (all route to TinkerClaw in mode 3)

| Input | Flow |
|-------|------|
| **Tab5 voice** | Tab5 mic → Dragon STT → transcript → TinkerClaw → response → Dragon TTS → Tab5 speaker |
| **Tab5 chat** | Tab5 keyboard → Dragon `text` handler → TinkerClaw → response → Dragon WS → Tab5 screen |
| **Dashboard chat** | Browser → Dragon `/api/v1/sessions/{id}/chat` → TinkerClaw → SSE stream → browser |

### What Dragon does in mode 3

- STT (configurable: Moonshine or OpenRouter)
- TTS (configurable: Piper or OpenRouter)
- WebSocket protocol with Tab5 (unchanged)
- Streams `llm` tokens to Tab5 as they arrive from TinkerClaw
- Sends `llm_done`, `tts_start`, binary audio, `tts_end`

### What Dragon does NOT do in mode 3

- No ConversationEngine (TinkerClaw owns conversation state)
- No ToolRegistry (TinkerClaw has its own skills)
- No MemoryService (TinkerClaw has its own memory)
- No message storage in Dragon DB (TinkerClaw stores JSONL transcripts)
- No context building or system prompt injection

### What TinkerClaw does in mode 3

- Receives user text via `/v1/chat/completions` with `user` field as session key
- Maintains conversation history internally (JSONL per session)
- Runs agent with skills (web_search, memory, browser, etc.)
- Selects LLM provider (Ollama local, OpenRouter cloud, etc.)
- Manages its own memory (sqlite-vec + BM25 hybrid search)
- Streams response tokens back via SSE

---

## Phase 1 Scope (this spec)

### 1. Fork: OpenClaw → TinkerClaw

**New repo:** `lorcan35/TinkerClaw` on GitHub

**What changes from OpenClaw:**
- Package name: `openclaw` → `tinkerclaw`
- Config path: `~/.openclaw/` → `~/.tinkerclaw/`
- Branding strings (about, help, user-agent headers)
- Default config tuned for Dragon deployment (loopback bind, Ollama provider)
- systemd service file: `tinkerclaw-gateway.service`

**What stays identical:**
- All channel adapters (Telegram, WhatsApp, Discord, Slack, Signal, LINE, etc.)
- All 50+ skills
- Browser automation (Playwright/CDP)
- Plugin/extension system (30+ extensions)
- Agent runner (Pi embedded runner)
- Memory system (sqlite-vec + BM25)
- All LLM providers (Ollama, OpenRouter, Anthropic, OpenAI, Google, etc.)
- TUI/CLI
- `/v1/chat/completions` HTTP endpoint

### 2. Install on Dragon

**Requirements:**
- Node.js 22+ ARM64 (install via NodeSource or nvm)
- pnpm 10+ (`npm install -g pnpm`)
- ~1.2GB disk (production deps)
- ~200MB RAM idle

**systemd service:** `tinkerclaw-gateway.service`
```ini
[Unit]
Description=TinkerClaw Agent Gateway
After=network.target ollama.service

[Service]
Type=simple
User=radxa
WorkingDirectory=/home/radxa/tinkerclaw
ExecStart=/usr/bin/node src/index.js gateway --port 18789
Restart=on-failure
RestartSec=5
Environment=NODE_ENV=production

[Install]
WantedBy=multi-user.target
```

**Config:** `~/.tinkerclaw/config.json`
```json
{
  "gateway": {
    "bind": "loopback",
    "port": 18789,
    "auth": { "mode": "token", "token": "<generated>" }
  },
  "models": {
    "providers": {
      "ollama": { "baseUrl": "http://localhost:11434" },
      "openrouter": { "apiKey": "<from-env>" }
    },
    "default": "ollama/qwen3:1.7b"
  },
  "memory": {
    "enabled": true,
    "provider": "ollama"
  }
}
```

### 3. Dragon LLM Backend: `tinkerclaw_llm.py`

**New file:** `dragon_voice/llm/tinkerclaw_llm.py`

Implements `LLMBackend` abstract class:

```python
class TinkerClawBackend(LLMBackend):
    def __init__(self, config: LLMConfig):
        self._url = config.tinkerclaw_url       # http://localhost:18789
        self._token = config.tinkerclaw_token   # gateway auth token
        self._model = config.tinkerclaw_model   # e.g. "ollama/qwen3:1.7b"
        self._session = None                     # aiohttp session

    async def initialize(self) -> None:
        # Create aiohttp session with auth header
        # Health check: GET /health on gateway

    async def generate_stream_with_messages(
        self, messages: list[dict]
    ) -> AsyncIterator[str]:
        # POST /v1/chat/completions
        # body: {model, messages, stream: true, user: session_key}
        # Parse SSE stream, yield tokens
        # Handle errors gracefully (gateway down → error message)

    async def shutdown(self) -> None:
        # Close aiohttp session
```

**Session key passing:** Dragon extracts `session_id` from `conn_state` and passes it as the `user` field in the chat completions request. TinkerClaw uses this to maintain conversation continuity.

### 4. Dragon `config.py` Changes

```python
@dataclass
class LLMConfig:
    # ... existing fields ...
    # New TinkerClaw fields:
    tinkerclaw_url: str = "http://localhost:18789"
    tinkerclaw_token: str = ""
    tinkerclaw_model: str = "ollama/qwen3:1.7b"
```

Valid backends list: `("ollama", "openrouter", "lmstudio", "npu_genie", "tinkerclaw")`

### 5. Dragon `server.py` Changes — Mode 3 Handler

In the `config_update` handler, when `voice_mode == 3`:

```python
if voice_mode == 3:
    # TinkerClaw mode — Dragon is audio pipe only
    # STT/TTS default to local (free), but user can override via llm_model field:
    # - "tinkerclaw:cloud" → use OpenRouter STT+TTS (same as Hybrid)
    # - "tinkerclaw:local" or just "tinkerclaw" → use Moonshine+Piper
    stt_be = "moonshine"
    tts_be = "piper"
    if llm_model and "cloud" in llm_model:
        stt_be = "openrouter"
        tts_be = "openrouter"
    llm_be = "tinkerclaw"
    # System prompt NOT set — TinkerClaw owns personality
    # max_tokens NOT set — TinkerClaw decides
```

**Pipeline bypass:** When `llm_be == "tinkerclaw"`, the pipeline skips Dragon's ConversationEngine entirely. Instead of:
```python
llm_stream = self._conversation_engine.process_text_stream(...)
```
It does:
```python
llm_stream = self._llm.generate_stream_with_messages([
    {"role": "user", "content": transcript}
])
```

Dragon sends only the latest user message. TinkerClaw maintains full context internally.

**Text input bypass:** Similarly, `_handle_text` in mode 3 calls `tinkerclaw_llm` directly instead of ConversationEngine.

### 6. Tab5 Changes — Voice Mode 3

**voice.h:** No enum change needed — voice_mode is already `uint8_t` (0-255). Mode 3 just works.

**settings.c:** `tab5_settings_get_voice_mode()` already returns uint8. Range check expands from 0-2 to 0-3.

**ui_settings.c:** Add 4th tab in Voice Mode section:
- **TinkerClaw tab** (red/gold `#E11D48` accent)
- Shows: "Full agent mode — skills, memory, cross-channel"
- Model dropdown: same as Cloud (Ollama models + OpenRouter models)
- Only enabled when TinkerClaw gateway is detected on Dragon

**ui_chat.c:** Mode badge cycles 0→1→2→3→0. Label: "TinkerClaw" with red accent.

**voice.c:** `voice_send_config_update(3, model)` sends `{"type":"config_update","voice_mode":3,"llm_model":"..."}`.

**config.h:** Add `#define VOICE_MODE_TINKERCLAW 3`.

---

## Phase 2 (future, not in this spec)

- **Tab5 channel adapter** — generic ESP device channel in TinkerClaw. Any ESP32 device can register. TinkerClaw WS connection to Dragon for push notifications.
- **Cross-channel routing** — Telegram message → TinkerClaw agent → push to Tab5 screen
- **Tool event streaming** — TinkerClaw streams skill invocation events to Dragon → Tab5 shows indicators
- **Multi-device** — multiple Tab5/ESP32 devices as separate TinkerClaw channel accounts

---

## Testing Plan

### Dragon-side
- [ ] TinkerClaw gateway starts on port 18789 (health check)
- [ ] `tinkerclaw_llm.py` connects and streams tokens
- [ ] Mode 3 config_update accepted by Dragon, ACK sent to Tab5
- [ ] Voice path: mic → STT → TinkerClaw → TTS → speaker (end-to-end)
- [ ] Text path: keyboard → TinkerClaw → text response on screen
- [ ] Dashboard chat: browser → TinkerClaw → SSE stream
- [ ] Mode switch: 0→3→0 without crash or state leak
- [ ] TinkerClaw down: graceful error message, Tab5 shows "TinkerClaw unavailable"
- [ ] Reconnect: voice WS reconnect restores mode 3 config

### Tab5-side
- [ ] Settings shows 4th TinkerClaw tab
- [ ] Mode badge cycles through 4 modes
- [ ] Model picker works in TinkerClaw mode
- [ ] No crash on rapid mode cycling (0→1→2→3→0)
- [ ] Voice overlay works normally in mode 3

### TinkerClaw-side
- [ ] Gateway starts on Dragon ARM64
- [ ] Ollama provider connects (localhost:11434)
- [ ] Memory system initializes with nomic-embed
- [ ] `/v1/chat/completions` returns streamed response
- [ ] Session continuity: multi-turn conversation maintains context
- [ ] Skills work: web_search, memory recall, datetime

---

## Risks

| Risk | Mitigation |
|------|-----------|
| TinkerClaw gateway uses too much RAM on Dragon | Monitor with systemd MemoryMax=2G. Strip optional plugins if needed. |
| Node.js 22 ARM64 has native module issues | sqlite-vec has prebuilt ARM64 binaries. Test early. |
| TinkerClaw gateway crashes → Tab5 stuck | Dragon detects HTTP error, sends error to Tab5, auto-reverts to Local mode (same pattern as cloud fallback). |
| Latency: Dragon STT → HTTP to TinkerClaw → TTS adds overhead | TinkerClaw is localhost, adds <10ms. Dominated by LLM inference time. |
| OpenClaw upstream changes break fork | Minimal rebrand = easy rebase. Core code untouched. |
