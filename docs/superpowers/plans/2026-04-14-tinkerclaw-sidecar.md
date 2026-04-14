# TinkerClaw Sidecar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fork OpenClaw as TinkerClaw, install on Dragon as a sidecar, add voice mode 3 to Dragon+Tab5 that routes through TinkerClaw gateway.

**Architecture:** Dragon voice server (port 3502) keeps STT/TTS. New `tinkerclaw_llm.py` backend calls TinkerClaw gateway (localhost:18789) for LLM. Tab5 gets 4th voice mode. TinkerClaw owns conversation state, skills, memory when active.

**Tech Stack:** Node.js 22 (TinkerClaw), Python 3.12 (Dragon), C/ESP-IDF (Tab5), aiohttp, pnpm

---

## File Structure

### TinkerClaw (new repo — `lorcan35/TinkerClaw`)
- Modify: `package.json` — name, homepage, bugs, repository
- Modify: `src/config/paths.ts:22-23` — `.openclaw` → `.tinkerclaw`, `openclaw.json` → `tinkerclaw.json`
- Modify: `src/index.ts:54,59` — error prefix `[openclaw]` → `[tinkerclaw]`
- Create: `systemd/tinkerclaw-gateway.service`
- Create: `dragon-config.json` — default config for Dragon deployment

### TinkerBox (Dragon — `lorcan35/TinkerBox`)
- Create: `dragon_voice/llm/tinkerclaw_llm.py`
- Modify: `dragon_voice/llm/__init__.py:6-11` — add tinkerclaw to `_BACKENDS`
- Modify: `dragon_voice/config.py:84-102` — add tinkerclaw fields to `LLMConfig`
- Modify: `dragon_voice/server.py:613-721` — add voice_mode 3 handling
- Modify: `dragon_voice/pipeline.py:407-422` — add tinkerclaw bypass path
- Modify: `CLAUDE.md` — document TinkerClaw mode
- Modify: `LEARNINGS.md` — add integration notes

### TinkerTab (Tab5 — `lorcan35/TinkerTab`)
- Modify: `main/config.h` — add `VOICE_MODE_TINKERCLAW`
- Modify: `main/ui_settings.c` — add 4th TinkerClaw tab
- Modify: `main/ui_chat.c` — mode badge cycles 0→1→2→3→0, model lists
- Modify: `main/voice.c` — response timeout for mode 3
- Modify: `CLAUDE.md` — document TinkerClaw mode

---

### Task 1: Create TinkerClaw GitHub Repo

**Files:**
- Create: GitHub repo `lorcan35/TinkerClaw`

- [ ] **Step 1: Create repo on GitHub**

```bash
gh repo create lorcan35/TinkerClaw --public --description "TinkerClaw — Agent gateway for the TinkerClaw ecosystem. Forked from OpenClaw." --clone=false
```

- [ ] **Step 2: Copy OpenClaw source to TinkerClaw**

```bash
cp -r /home/rebelforce/Desktop/openclaw /home/rebelforce/projects/TinkerClaw
cd /home/rebelforce/projects/TinkerClaw
rm -rf .git
git init
git remote add origin https://github.com/lorcan35/TinkerClaw.git
```

- [ ] **Step 3: Commit initial fork**

```bash
git add -A
git commit -m "Initial fork from OpenClaw (github.com/peterSteinberger/openclaw)"
git branch -M main
git push -u origin main
```

---

### Task 2: Rebrand OpenClaw → TinkerClaw

**Files:**
- Modify: `package.json`
- Modify: `src/config/paths.ts:22-23`
- Modify: `src/index.ts:54,59`

- [ ] **Step 1: Rebrand package.json**

In `/home/rebelforce/projects/TinkerClaw/package.json`, change:
```json
"name": "openclaw" → "name": "tinkerclaw"
"homepage": "https://github.com/openclaw/openclaw#readme" → "https://github.com/lorcan35/TinkerClaw#readme"
"url": "https://github.com/openclaw/openclaw/issues" → "https://github.com/lorcan35/TinkerClaw/issues"
"url": "git+https://github.com/openclaw/openclaw.git" → "git+https://github.com/lorcan35/TinkerClaw.git"
"openclaw": "openclaw.mjs" → "tinkerclaw": "tinkerclaw.mjs"
```

- [ ] **Step 2: Rebrand config paths**

In `src/config/paths.ts`, change lines 22-23:
```typescript
const NEW_STATE_DIRNAME = ".tinkerclaw";
const CONFIG_FILENAME = "tinkerclaw.json";
```

Also change all `OPENCLAW_` env var prefixes to `TINKERCLAW_`:
- `OPENCLAW_NIX_MODE` → `TINKERCLAW_NIX_MODE`
- `OPENCLAW_STATE_DIR` → `TINKERCLAW_STATE_DIR`
- `OPENCLAW_CONFIG_PATH` → `TINKERCLAW_CONFIG_PATH`
- `OPENCLAW_HOME` → `TINKERCLAW_HOME`
- (Keep `CLAWDBOT_*` compat aliases unchanged)

- [ ] **Step 3: Rebrand error prefix in index.ts**

In `src/index.ts`, change:
```typescript
"[openclaw] Uncaught exception:" → "[tinkerclaw] Uncaught exception:"
"[openclaw] CLI failed:" → "[tinkerclaw] CLI failed:"
```

- [ ] **Step 4: Rebrand env vars across codebase**

```bash
cd /home/rebelforce/projects/TinkerClaw
# Find all OPENCLAW_ references (excluding node_modules, .git, tests)
grep -rn "OPENCLAW_" src/ --include="*.ts" | grep -v "node_modules" | grep -v ".test." | head -30
# Replace OPENCLAW_ → TINKERCLAW_ in non-test source files
find src/ -name "*.ts" ! -name "*.test.*" -exec sed -i 's/OPENCLAW_/TINKERCLAW_/g' {} +
```

- [ ] **Step 5: Commit rebrand**

```bash
git add -A
git commit -m "rebrand: openclaw → tinkerclaw (package name, config paths, env vars)"
```

---

### Task 3: Add Dragon Deployment Config

**Files:**
- Create: `dragon-config.json`
- Create: `systemd/tinkerclaw-gateway.service`

- [ ] **Step 1: Create default Dragon config**

Create `/home/rebelforce/projects/TinkerClaw/dragon-config.json`:
```json
{
  "gateway": {
    "bind": "loopback",
    "port": 18789,
    "auth": {
      "mode": "token"
    }
  },
  "models": {
    "providers": {
      "ollama": {
        "baseUrl": "http://localhost:11434"
      }
    },
    "default": "ollama/qwen3:1.7b"
  },
  "memory": {
    "enabled": true,
    "provider": "ollama"
  }
}
```

- [ ] **Step 2: Create systemd service file**

Create `/home/rebelforce/projects/TinkerClaw/systemd/tinkerclaw-gateway.service`:
```ini
[Unit]
Description=TinkerClaw Agent Gateway
After=network.target ollama.service
Wants=ollama.service

[Service]
Type=simple
User=radxa
WorkingDirectory=/home/radxa/tinkerclaw
ExecStart=/usr/bin/node dist/index.js gateway --port 18789
Restart=on-failure
RestartSec=5
Environment=NODE_ENV=production
Environment=TINKERCLAW_STATE_DIR=/home/radxa/.tinkerclaw
MemoryMax=2G

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 3: Commit**

```bash
git add dragon-config.json systemd/
git commit -m "feat: Dragon deployment config + systemd service"
git push origin main
```

---

### Task 4: Dragon LLM Backend — `tinkerclaw_llm.py`

**Files:**
- Create: `dragon_voice/llm/tinkerclaw_llm.py`
- Modify: `dragon_voice/llm/__init__.py:6-11`

- [ ] **Step 1: Create TinkerClaw LLM backend**

Create `/home/rebelforce/projects/TinkerBox/dragon_voice/llm/tinkerclaw_llm.py`:
```python
"""TinkerClaw gateway LLM backend.

Routes LLM inference to the local TinkerClaw agent gateway via its
OpenAI-compatible /v1/chat/completions endpoint. TinkerClaw maintains
its own conversation state, skills, and memory — Dragon just pipes
audio and streams text.
"""

import json
import logging
from typing import AsyncIterator, Optional

import aiohttp

from dragon_voice.config import LLMConfig
from dragon_voice.llm.base import LLMBackend

logger = logging.getLogger(__name__)


class TinkerClawBackend(LLMBackend):
    """LLM backend that proxies to a local TinkerClaw agent gateway."""

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._url = (config.tinkerclaw_url or "http://localhost:18789").rstrip("/")
        self._token = config.tinkerclaw_token
        self._model = config.tinkerclaw_model or "ollama/qwen3:1.7b"
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_key: Optional[str] = None  # Set per-request by pipeline

    async def initialize(self) -> None:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=300, sock_read=120),
            headers=headers,
        )

        # Health check — verify gateway is running
        try:
            async with self._session.get(f"{self._url}/health") as resp:
                if resp.status == 200:
                    logger.info("TinkerClaw gateway connected at %s", self._url)
                else:
                    logger.warning("TinkerClaw health check returned %d", resp.status)
        except aiohttp.ClientError as e:
            logger.warning("TinkerClaw gateway not reachable at %s: %s", self._url, e)
            # Don't raise — gateway might start later. Will fail on first request.

    def set_session_key(self, session_key: str) -> None:
        """Set the session key for the next request. Called by pipeline/server."""
        self._session_key = session_key

    async def generate_stream(
        self, prompt: str, system_prompt: str = ""
    ) -> AsyncIterator[str]:
        """Stream tokens from TinkerClaw for a single prompt."""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        async for token in self.generate_stream_with_messages(messages):
            yield token

    async def generate_stream_with_messages(
        self, messages: list[dict]
    ) -> AsyncIterator[str]:
        """Stream tokens from TinkerClaw using OpenAI-format messages.

        TinkerClaw maintains its own conversation history via the session key.
        Dragon sends only the latest user message — TinkerClaw handles context.
        """
        if not self._session or self._session.closed:
            await self.initialize()

        payload = {
            "model": self._model,
            "messages": messages,
            "stream": True,
        }
        if self._session_key:
            payload["user"] = self._session_key

        try:
            async with self._session.post(
                f"{self._url}/v1/chat/completions",
                json=payload,
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error("TinkerClaw error %d: %s", resp.status, error_text[:300])
                    yield f"[TinkerClaw error: {resp.status}]"
                    return

                # Parse SSE stream
                async for line in resp.content:
                    line = line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data: "):
                        continue

                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break

                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    choices = chunk.get("choices", [])
                    if not choices:
                        continue

                    delta = choices[0].get("delta", {})
                    token = delta.get("content", "")
                    if token:
                        yield token

        except aiohttp.ClientError as e:
            logger.error("TinkerClaw request failed: %s", e)
            yield "[TinkerClaw unavailable — check gateway]"

    async def shutdown(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        logger.info("TinkerClaw backend shut down")

    @property
    def name(self) -> str:
        return f"TinkerClaw ({self._model})"
```

- [ ] **Step 2: Register backend in factory**

In `/home/rebelforce/projects/TinkerBox/dragon_voice/llm/__init__.py`, add to `_BACKENDS`:
```python
_BACKENDS = {
    "ollama": "dragon_voice.llm.ollama_llm.OllamaBackend",
    "openrouter": "dragon_voice.llm.openrouter_llm.OpenRouterBackend",
    "lmstudio": "dragon_voice.llm.lmstudio_llm.LMStudioBackend",
    "npu_genie": "dragon_voice.llm.npu_genie.NPUGenieBackend",
    "tinkerclaw": "dragon_voice.llm.tinkerclaw_llm.TinkerClawBackend",
}
```

- [ ] **Step 3: Commit**

```bash
cd /home/rebelforce/projects/TinkerBox
git add dragon_voice/llm/tinkerclaw_llm.py dragon_voice/llm/__init__.py
git commit -m "feat: TinkerClaw LLM backend — proxies to local agent gateway"
```

---

### Task 5: Dragon Config — Add TinkerClaw Fields

**Files:**
- Modify: `dragon_voice/config.py:84-102`

- [ ] **Step 1: Add TinkerClaw fields to LLMConfig**

In `/home/rebelforce/projects/TinkerBox/dragon_voice/config.py`, add after `temperature: float = 0.7` (line 102):
```python
    # TinkerClaw agent gateway (optional sidecar)
    tinkerclaw_url: str = "http://localhost:18789"
    tinkerclaw_token: str = ""
    tinkerclaw_model: str = "ollama/qwen3:1.7b"
```

- [ ] **Step 2: Commit**

```bash
git add dragon_voice/config.py
git commit -m "feat: add TinkerClaw config fields to LLMConfig"
```

---

### Task 6: Dragon Server — Voice Mode 3 Handler

**Files:**
- Modify: `dragon_voice/server.py:613-721` — config_update handler
- Modify: `dragon_voice/server.py:862+` — _handle_text bypass
- Modify: `dragon_voice/pipeline.py:407-422` — LLM path bypass

- [ ] **Step 1: Add mode 3 to config_update handler**

In `dragon_voice/server.py`, in the `config_update` handler (after the `voice_mode == 2` block at ~line 624), add a new elif:

```python
                            elif voice_mode == 3:
                                # TinkerClaw mode — Dragon is audio/text pipe only
                                llm_be = "tinkerclaw"
                                # STT/TTS: default local, "cloud" suffix → OpenRouter
                                if llm_model and "cloud" in llm_model.lower():
                                    stt_be, tts_be = "openrouter", "openrouter"
                                else:
                                    stt_be, tts_be = "moonshine", "piper"
                                # TinkerClaw owns personality — don't set system prompt
                                # Don't set max_tokens — TinkerClaw decides
```

- [ ] **Step 2: Pass session_key to TinkerClaw backend**

In `server.py`, after the pipeline swap (around line 690), add session key injection:
```python
                            # If TinkerClaw, inject session key for conversation continuity
                            if llm_be == "tinkerclaw" and pipeline and hasattr(pipeline, '_llm'):
                                if hasattr(pipeline._llm, 'set_session_key'):
                                    pipeline._llm.set_session_key(conn_state.get("session_id", ""))
```

- [ ] **Step 3: Add pipeline bypass for TinkerClaw mode**

In `dragon_voice/pipeline.py`, modify the LLM path selection (around line 407):

Replace:
```python
            if self._conversation_engine and self._session_id:
```

With:
```python
            if self._config.llm.backend == "tinkerclaw":
                # TinkerClaw mode: bypass ConversationEngine entirely.
                # Send only the latest user message — TinkerClaw owns context.
                if hasattr(self._llm, 'set_session_key') and self._session_id:
                    self._llm.set_session_key(self._session_id)
                llm_stream = self._llm.generate_stream_with_messages([
                    {"role": "user", "content": transcript}
                ])
            elif self._conversation_engine and self._session_id:
```

- [ ] **Step 4: Add _handle_text bypass for TinkerClaw mode**

In `server.py`, in `_handle_text` method, add TinkerClaw bypass before the ConversationEngine call. Find the section where `self._conversation.process_text_stream()` is called and add:

```python
                        # TinkerClaw mode: bypass ConversationEngine
                        if self._config.llm.backend == "tinkerclaw":
                            pipeline = conn_state.get("pipeline")
                            if pipeline and pipeline._llm:
                                if hasattr(pipeline._llm, 'set_session_key'):
                                    pipeline._llm.set_session_key(conn_state.get("session_id", ""))
                                llm_stream = pipeline._llm.generate_stream_with_messages([
                                    {"role": "user", "content": text}
                                ])
                                full_response = []
                                async for token in llm_stream:
                                    full_response.append(token)
                                    if not ws.closed:
                                        await ws.send_json({"type": "llm", "text": token})
                                if not ws.closed:
                                    await ws.send_json({"type": "llm_done",
                                        "llm_ms": 0, "text": "".join(full_response)})
                                return
```

- [ ] **Step 5: Add fallback error handling for TinkerClaw down**

In `pipeline.py`, after the TinkerClaw LLM stream section, the existing hallucination/sentence-buffer logic handles errors. But add a specific catch in the pipeline for gateway unavailability:

The existing `except Exception` block at the end of `_process_utterance` already handles this:
```python
        except Exception:
            logger.exception("Pipeline processing error")
            try:
                await self._on_event(
                    {"type": "error", "message": "Processing failed — see server logs"}
                )
```

This is sufficient — if TinkerClaw is down, the `generate_stream_with_messages` yields an error message token `[TinkerClaw unavailable]`, which gets spoken via TTS. The user hears the error.

- [ ] **Step 6: Commit**

```bash
git add dragon_voice/server.py dragon_voice/pipeline.py
git commit -m "feat: voice mode 3 (TinkerClaw) — bypass ConversationEngine, route to gateway"
```

---

### Task 7: Tab5 — Voice Mode 3 UI

**Files:**
- Modify: `main/config.h`
- Modify: `main/ui_settings.c`
- Modify: `main/ui_chat.c`
- Modify: `main/voice.c`

- [ ] **Step 1: Add mode constant to config.h**

In `/home/rebelforce/projects/TinkerTab/main/config.h`, add near other voice mode defines:
```c
#define VOICE_MODE_TINKERCLAW 3
```

- [ ] **Step 2: Update mode badge in ui_chat.c**

In `ui_chat.c`, find `cb_mode_cycle` and change the modulo:
```c
mode = (mode + 1) % 4;  // was % 3
```

In `update_mode_badge_obj`, add case for mode 3:
```c
case 3:
    lv_label_set_text(badge, "TinkerClaw");
    lv_obj_set_style_text_color(badge, lv_color_hex(0xE11D48), 0);
    lv_obj_set_style_bg_color(badge, lv_color_hex(0x3B0716), 0);
    break;
```

In `cb_model_cycle`, add TinkerClaw model list:
```c
static const char *s_tinkerclaw_models[] = {
    "ollama/qwen3:1.7b", "ollama/qwen3:4b",
    "anthropic/claude-3.5-haiku", "anthropic/claude-sonnet-4-20250514",
    "openai/gpt-4o-mini",
};
#define N_TINKERCLAW_MODELS (sizeof(s_tinkerclaw_models) / sizeof(s_tinkerclaw_models[0]))
```

And in the model selection logic:
```c
if (mode == 3) {
    models = s_tinkerclaw_models;
    n = N_TINKERCLAW_MODELS;
} else if (mode == 2) {
```

- [ ] **Step 3: Add TinkerClaw tab in ui_settings.c**

Add a 4th tab button after the Cloud tab in the Voice Mode section. Use accent color `#E11D48` (rose). The tab shows:
- "Agent mode — skills, memory, cross-channel"
- Model dropdown same as TinkerClaw model list

- [ ] **Step 4: Update voice.c response timeout for mode 3**

In `voice.c`, where response timeout is set based on mode, add mode 3:
```c
// TinkerClaw mode: 5 min timeout (agent skills can take time)
if (saved_mode == 3) {
    response_timeout_ms = 300000;
}
```

- [ ] **Step 5: Build and verify compilation**

```bash
cd /home/rebelforce/projects/TinkerTab
source /home/rebelforce/esp/esp-idf-v5.4.3/export.sh
idf.py build
```

- [ ] **Step 6: Commit**

```bash
git add main/config.h main/ui_settings.c main/ui_chat.c main/voice.c
git commit -m "feat: voice mode 3 (TinkerClaw) — 4th tab, mode badge, model picker"
```

---

### Task 8: Install TinkerClaw on Dragon

- [ ] **Step 1: Install Node.js 22 on Dragon**

```bash
sshpass -p 'radxa' ssh radxa@192.168.1.91 "curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt-get install -y nodejs && node --version && npm --version"
```

- [ ] **Step 2: Install pnpm**

```bash
sshpass -p 'radxa' ssh radxa@192.168.1.91 "sudo npm install -g pnpm && pnpm --version"
```

- [ ] **Step 3: Clone TinkerClaw to Dragon**

```bash
sshpass -p 'radxa' ssh radxa@192.168.1.91 "cd /home/radxa && git clone https://github.com/lorcan35/TinkerClaw.git tinkerclaw"
```

- [ ] **Step 4: Install dependencies**

```bash
sshpass -p 'radxa' ssh radxa@192.168.1.91 "cd /home/radxa/tinkerclaw && pnpm install --prod 2>&1 | tail -10"
```

- [ ] **Step 5: Build (if TypeScript needs compilation)**

```bash
sshpass -p 'radxa' ssh radxa@192.168.1.91 "cd /home/radxa/tinkerclaw && pnpm build 2>&1 | tail -10"
```

- [ ] **Step 6: Create config directory and copy Dragon config**

```bash
sshpass -p 'radxa' ssh radxa@192.168.1.91 "mkdir -p ~/.tinkerclaw && cp /home/radxa/tinkerclaw/dragon-config.json ~/.tinkerclaw/tinkerclaw.json"
```

- [ ] **Step 7: Test gateway starts**

```bash
sshpass -p 'radxa' ssh radxa@192.168.1.91 "cd /home/radxa/tinkerclaw && timeout 10 node dist/index.js gateway --port 18789 2>&1 | head -20"
```

- [ ] **Step 8: Install systemd service**

```bash
sshpass -p 'radxa' ssh radxa@192.168.1.91 "echo 'radxa' | sudo -S cp /home/radxa/tinkerclaw/systemd/tinkerclaw-gateway.service /etc/systemd/system/ && echo 'radxa' | sudo -S systemctl daemon-reload && echo 'radxa' | sudo -S systemctl enable tinkerclaw-gateway && echo 'radxa' | sudo -S systemctl start tinkerclaw-gateway"
```

- [ ] **Step 9: Verify gateway is running**

```bash
sshpass -p 'radxa' ssh radxa@192.168.1.91 "systemctl status tinkerclaw-gateway --no-pager; curl -s http://localhost:18789/health"
```

---

### Task 9: Deploy Dragon Changes and Test

- [ ] **Step 1: Deploy Dragon voice server changes**

```bash
cd /home/rebelforce/projects/TinkerBox
sshpass -p 'radxa' scp -r dragon_voice/ radxa@192.168.1.91:/home/radxa/
sshpass -p 'radxa' ssh radxa@192.168.1.91 "find /home/radxa/dragon_voice -name '__pycache__' -exec rm -rf {} + 2>/dev/null; echo 'radxa' | sudo -S systemctl restart tinkerclaw-voice"
```

- [ ] **Step 2: Test mode 3 config_update via WebSocket**

```bash
# From workstation, use the dashboard or curl to send a config_update
curl -s -X POST http://192.168.1.90:8080/mode?m=3 2>&1
```

Check Dragon logs:
```bash
sshpass -p 'radxa' ssh radxa@192.168.1.91 "journalctl -u tinkerclaw-voice --no-pager -n 10 | grep -i tinkerclaw"
```

- [ ] **Step 3: Test TinkerClaw LLM backend directly**

```bash
# Call TinkerClaw gateway directly from Dragon
sshpass -p 'radxa' ssh radxa@192.168.1.91 'curl -s -X POST http://localhost:18789/v1/chat/completions -H "Content-Type: application/json" -d "{\"model\":\"ollama/qwen3:1.7b\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello\"}],\"stream\":false}" 2>&1 | head -20'
```

- [ ] **Step 4: Test text input via Tab5 debug server**

```bash
curl -s -X POST http://192.168.1.90:8080/chat -d '{"text":"What is 2+2?"}' 2>&1
```

- [ ] **Step 5: Test mode switching 0→3→0**

```bash
curl -s -X POST http://192.168.1.90:8080/mode?m=3
sleep 2
curl -s http://192.168.1.90:8080/voice
sleep 1
curl -s -X POST http://192.168.1.90:8080/mode?m=0
sleep 2
curl -s http://192.168.1.90:8080/voice
```

---

### Task 10: Flash Tab5 and End-to-End Test

- [ ] **Step 1: Build and flash Tab5**

```bash
cd /home/rebelforce/projects/TinkerTab
source /home/rebelforce/esp/esp-idf-v5.4.3/export.sh
idf.py build && idf.py -p /dev/ttyACM0 flash
```

- [ ] **Step 2: Verify boot and mode 3 config restore**

Monitor serial for successful boot, voice WS connection, and config_update:
```bash
# Watch serial for 30s after flash
python3 -c "import serial,time; s=serial.Serial('/dev/ttyACM0',115200,timeout=2); [print(s.read(s.in_waiting).decode('utf-8',errors='replace'),end='',flush=True) or time.sleep(0.1) for _ in range(300)]"
```

- [ ] **Step 3: Test mode cycling via touch**

Navigate to Chat, tap mode badge 4 times (0→1→2→3→0). Verify each mode shows correct label and no crash.

- [ ] **Step 4: Test voice in TinkerClaw mode**

Switch to mode 3. Tap mic. Say "What time is it?". Verify:
- STT transcribes correctly
- TinkerClaw responds (may use datetime skill)
- TTS speaks the response

- [ ] **Step 5: Test text in TinkerClaw mode**

Switch to mode 3. Open Chat. Type "Hello Tinker". Verify streamed text response appears.

- [ ] **Step 6: Test TinkerClaw down gracefully**

```bash
# Stop TinkerClaw gateway
sshpass -p 'radxa' ssh radxa@192.168.1.91 "echo 'radxa' | sudo -S systemctl stop tinkerclaw-gateway"
# Send a message — should get error
curl -s -X POST http://192.168.1.90:8080/chat -d '{"text":"Hello"}'
# Verify Tab5 shows error, doesn't crash
# Restart gateway
sshpass -p 'radxa' ssh radxa@192.168.1.91 "echo 'radxa' | sudo -S systemctl start tinkerclaw-gateway"
```

---

### Task 11: Update Documentation — All Three Repos

**Files:**
- Modify: TinkerBox `CLAUDE.md`, `LEARNINGS.md`
- Modify: TinkerTab `CLAUDE.md`, `LEARNINGS.md`
- Create: TinkerClaw `README.md`

- [ ] **Step 1: Update TinkerBox CLAUDE.md**

Add to the Three-Tier Voice Mode table:
```markdown
| **TinkerClaw** | 3 | Moonshine (or OpenRouter) | TinkerClaw Gateway (agent runner) | Piper (or OpenRouter) |
```

Add a new section:
```markdown
## TinkerClaw Integration (Optional Sidecar)
- **Port:** 18789 (localhost only)
- **Service:** tinkerclaw-gateway.service
- **Purpose:** Full agent gateway with 50+ skills, memory, channels. Dragon routes to it in voice_mode 3.
- **Dragon is audio pipe only:** STT/TTS handled by Dragon, everything else by TinkerClaw.
- **Fallback:** If gateway is down, Dragon returns error to Tab5 (same as cloud fallback pattern).
- **Config:** `~/.tinkerclaw/tinkerclaw.json` on Dragon
- **Session continuity:** Dragon passes session_id as `user` field to TinkerClaw's /v1/chat/completions
```

- [ ] **Step 2: Update TinkerTab CLAUDE.md**

Add voice mode 3 to the voice mode table and settings section.

- [ ] **Step 3: Add LEARNINGS entry to TinkerBox**

```markdown
### TinkerClaw Sidecar Integration
- **Date:** 2026-04-14
- **Symptom:** N/A (architecture decision)
- **Root Cause:** Dragon's ConversationEngine has 10 tools, simple memory. TinkerClaw (forked OpenClaw) has 50+ skills, hybrid memory, browser automation, channel routing.
- **Fix:** Voice mode 3 routes LLM through TinkerClaw gateway on localhost:18789. Dragon keeps STT/TTS. ConversationEngine bypassed entirely in mode 3.
- **Prevention:** TinkerClaw is optional — modes 0-2 work without it. If gateway is down, Dragon returns error (same pattern as cloud fallback).
```

- [ ] **Step 4: Create TinkerClaw README.md**

Write a README for the TinkerClaw repo explaining: what it is (forked OpenClaw), what it adds (Dragon integration, Tab5 channel future), how to deploy on Dragon, how to configure.

- [ ] **Step 5: Commit all docs**

```bash
# TinkerBox
cd /home/rebelforce/projects/TinkerBox
git add CLAUDE.md LEARNINGS.md
git commit -m "docs: TinkerClaw sidecar integration — mode 3, config, learnings"
git push origin main

# TinkerTab
cd /home/rebelforce/projects/TinkerTab
git add CLAUDE.md
git commit -m "docs: voice mode 3 (TinkerClaw) documentation"
git push origin main

# TinkerClaw
cd /home/rebelforce/projects/TinkerClaw
git add README.md
git commit -m "docs: TinkerClaw README — Dragon deployment guide"
git push origin main
```

---

## Self-Review Checklist

- **Spec coverage:** All 6 Phase 1 sections covered (fork, install, LLM backend, config, server changes, Tab5 UI). Testing plan tasks in Task 9-10.
- **Placeholder scan:** No TBDs. All code blocks are complete.
- **Type consistency:** `TinkerClawBackend` class, `set_session_key` method, `tinkerclaw_url`/`tinkerclaw_token`/`tinkerclaw_model` config fields used consistently across tasks 4-6.
- **Spec gaps:** None found — all spec sections have corresponding tasks.
