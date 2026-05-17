# TinkerBox — Dragon Server Stack (THE BRAIN)

> **Trying to understand what this project IS rather than how to operate it?** Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) first — system overview, component diagram, data flows. This file is the *runbook* (deploy, debug, restart, monitor); ARCHITECTURE.md is the *map*.  [`GLOSSARY.md`](GLOSSARY.md) covers any unfamiliar terms.

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
- **Host (current 2026-05-04):** `192.168.70.242` (LAN flips between `192.168.1.x` and `192.168.70.x` — historic IP `192.168.1.91` was valid on the .1.x LAN; verify with `ping radxa-dragon-q6a` or `nmap -p 22,3502,18789 --open <subnet>/24`)
- **Hostname:** `radxa-dragon-q6a` (nmap reverse DNS)
- **User:** radxa
- **Password:** `<DRAGON_SSH_PASSWORD>` (passes via `sshpass -p '<pw>'`; sudo on Dragon is currently passwordless after SSH login)
- **SSH:** `ssh radxa@192.168.70.242  # or whatever the current LAN IP is`
- **OS:** Ubuntu (Dragon Q6A — Qualcomm QCS6490, ARM64)
- **Connection:** Ethernet, DHCP-managed via enp1s0 (was documented as static — empirically the IP rotates between LANs, so treat as DHCP).
- **Services stripped:** gdm3, snapd, nanobot, rustdesk, fwupd masked.  Active services: tinkerclaw-voice, tinkerclaw-dashboard, tinkerclaw-ngrok, **ollama** (active for embeddings + Local-mode LLM inference; was previously masked but unmasked when the multi-model router landed in #185).

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
scp -r dragon_voice/ radxa@192.168.70.242:/home/radxa/
scp dashboard.py radxa@192.168.70.242:/home/radxa/
scp schema.sql radxa@192.168.70.242:/home/radxa/

# Restart services
ssh radxa@192.168.70.242 "sudo systemctl restart tinkerclaw-voice"
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
- **Valid backends:** STT: `moonshine`, `whisper_cpp`, `vosk`, `openrouter`. TTS: `piper`, `kokoro`, `edge_tts`, `openrouter`. LLM: `ollama`, `npu_genie`, `openrouter`, `lmstudio`, `tinkerclaw`, `dual`, `router` (#183).
- **Mode-aware system prompts:** Each voice mode sets a different system prompt length — Local (concise, 128 tokens), Hybrid (medium, 256 tokens), Cloud (rich, 512 tokens). This keeps local model context tight while giving cloud models room for nuanced instructions.
- **Mode-aware pipeline timeouts:** Local mode = 300s (5 min) for tool-calling chains on slow local models. Cloud mode = 60s (1 min). TinkerClaw mode = 180s (3 min, tool execution gaps). Timeouts configured per voice mode in pipeline.py.
- **Session system_prompt updated on mode switch:** When voice_mode changes, the session's `system_prompt` is updated in the DB immediately so the conversation engine picks it up on the next turn.
- **Pipeline init resets to local defaults on reconnect:** When a device reconnects, the pipeline is re-initialized with local defaults (voice_mode 0) regardless of the previous session's mode. The client must re-send `config_update` to restore cloud mode.
- **Per-connection config (deep copy):** Each WebSocket connection gets a deep copy of the global config via `copy.deepcopy()`. This prevents one device's config_update (e.g., switching to cloud mode) from corrupting another device's pipeline config. Without deep copy, two Tab5s connected simultaneously would share the same mutable config object.

## Multi-Model Router (#183, April 2026)

**The single voice-mode-picks-one-backend assumption is gone.** Dragon now supports a *fleet* of LLM backends declared in `LLMConfig.fleet`, with a `CapabilityAwareRouter` that picks per-turn based on the modalities present in the message and the active voice_mode tier.

The router is opt-in: keep `backend: "ollama"` (or any single backend) and behavior is identical to today. Set `backend: "router"` and populate `fleet` to activate.

### Capability declarations
Every `LLMBackend` subclass declares a frozenset of `Modality` values via the `capabilities` property.  See `dragon_voice/llm/base.py`.

| Modality | Used when |
|----------|-----------|
| `TEXT` | Always required |
| `VISION` | Message content has `{"type":"image_url"}` |
| `VIDEO` | Message content has `{"type":"video_url"}` |
| `AUDIO_IN` | Message content has `{"type":"input_audio"}` |
| `AUDIO_OUT` | Backend can emit audio responses |
| `TOOL_CALLING` | Backend trained for function-calling (informational; *not* a router gate) |

Per-backend declaration logic:
- **ollama** — substring scan of model id (vision: `llava`, `minicpm-v`, `minicpm-o`, `moondream`, `qwen2-vl`, `pixtral`, `internvl`; audio: `minicpm-o`; tools: known FC families).
- **openrouter** — static registry in `_OPENROUTER_CAPS` (`openrouter_llm.py`). 35 models tracked as of 2026-04-27 (see PR #187). Unknown models default to `{TEXT, TOOL_CALLING}`.
- **lmstudio** — same name-substring heuristic as ollama (LM Studio serves arbitrary GGUFs).
- **npu_genie** — text-only (QAIRT has no vision support).
- **tinkerclaw** — TEXT+TOOL_CALLING + VISION when gateway model is `anthropic/`, `openai/gpt-4o`, `google/gemini`, `minimax/`.
- **dual** — forwards to responder's caps (responder is the backend whose tokens reach the user).

### Tier policy from voice_mode
```
TIER_FOR_MODE = {
    0: {"local"},          # Local
    1: {"local"},          # Hybrid (LLM stays local; STT/TTS go cloud — unchanged)
    2: {"cloud", "lan"},   # Full Cloud (LAN tier eligible — e.g., LM Studio on workstation)
    3: None,               # TinkerClaw — bypass router, gateway picks
}
```

### Routing rule
```python
def choose(required_caps, voice_mode):
    tier_filter = TIER_FOR_MODE[voice_mode]
    if tier_filter is None:
        return None  # tinkerclaw mode — router not used
    candidates = [m for m in fleet
                  if required_caps <= m.capabilities
                  and m.tier in tier_filter]
    return min(candidates, key=lambda m: m.priority) if candidates else None
```

Lowest priority wins. `infer_required_caps(messages)` walks OpenAI-format content arrays — image_url → +VISION, video_url → +VIDEO, input_audio → +AUDIO_IN. TOOL_CALLING is intentionally NOT inferred (would gate vision turns from picking MiniCPM-V which lacks tools); tool detection happens at runtime when the model emits a tool marker.

### Fleet config example
Drop into `dragon_voice/config.yaml`. Verified live against OpenRouter 2026-04-27.

```yaml
llm:
  backend: "router"
  fleet:
    # ── Local tier ──
    - {id: ministral,    backend: ollama,     model_id: "ministral-3:3b",
       caps: [text, tool_calling],          tier: local, priority: 0,  keep_alive_s: 600}
    - {id: minicpm_v4,   backend: ollama,
       model_id: "hf.co/openbmb/MiniCPM-V-4-gguf:Q4_K_M",
       caps: [text, vision, video],         tier: local, priority: 10, keep_alive_s: 120}
    # ── Cloud tier ──
    - {id: ds_v4_flash,  backend: openrouter, model_id: "deepseek/deepseek-v4-flash",
       caps: [text, tool_calling],          tier: cloud, priority: 0}      # cheapest text+tools
    - {id: qwen36_flash, backend: openrouter, model_id: "qwen/qwen3.6-flash",
       caps: [text, vision, tool_calling],  tier: cloud, priority: 5}      # cheapest multimodal
    - {id: gemini_flash, backend: openrouter, model_id: "google/gemini-3-flash-preview",
       caps: [text, vision, video, audio_in, tool_calling],
                                            tier: cloud, priority: 8}      # native video
    - {id: sonnet_46,    backend: openrouter, model_id: "anthropic/claude-sonnet-4.6",
       caps: [text, vision, tool_calling],  tier: cloud, priority: 12}     # quality vision
    - {id: gemini_pro,   backend: openrouter, model_id: "google/gemini-3.1-pro-preview",
       caps: [text, vision, video, audio_in, tool_calling],
                                            tier: cloud, priority: 18}     # frontier multimodal
    - {id: opus_47,      backend: openrouter, model_id: "anthropic/claude-opus-4.7",
       caps: [text, vision, tool_calling],  tier: cloud, priority: 25}     # premium agentic
    - {id: gpt_55,       backend: openrouter, model_id: "openai/gpt-5.5",
       caps: [text, vision, tool_calling],  tier: cloud, priority: 30}     # frontier
```

### How the router behaves
- **Local mode, text turn:** picks `ministral` (priority 0).
- **Local mode, vision turn (photo):** picks `minicpm_v4` (only local-tier vision-capable).
- **Cloud mode, text turn:** picks `ds_v4_flash` ($0.14/$0.28 per M, 7× cheaper than Sonnet for plain chat).
- **Cloud mode, vision turn:** picks `qwen36_flash` ($0.25/$1.50, 1M context).
- **Cloud mode, video turn (.MJP from Tab5):** picks `gemini_flash` (only cloud-tier with VIDEO at priority ≤ 18).
- **Voice-mode swap (e.g. Local → Cloud):** router calls `set_voice_mode(2)` — no backend recreate, instantiated sub-backends survive the switch.

### `fleet_summary` in protocol
When the router is active, `session_start.config` and `config_update` ACK include a per-modality summary so Tab5 can light up vision/video/audio capability chips dynamically:
```json
{
  "type": "session_start",
  "config": {
    ...,
    "fleet_summary": {
      "text":     "ministral-3:3b",
      "vision":   "hf.co/openbmb/MiniCPM-V-4-gguf:Q4_K_M",
      "video":    "hf.co/openbmb/MiniCPM-V-4-gguf:Q4_K_M",
      "audio_in": null,
      "audio_out": null,
      "tool_calling": "ministral-3:3b"
    }
  }
}
```
Tab5 firmware can ignore `fleet_summary` (legacy `vision_capability` event keeps firing for backward compat).

### Cross-modal continuity
`_handle_user_media` no longer bypasses ConversationEngine. Multimodal user messages persist via `MessageStore.add_message(media_id=...)` with a `__mm__:` JSON marker. `get_context(media_store=...)` hydrates them back to OpenAI `image_url` content arrays on context build. **Result: send a photo, then ask "what color was the chair?" — the text follow-up turn still sees the photo.**

`OllamaBackend.generate_stream_with_messages` translates OpenAI-format multimodal content arrays to Ollama's flat `content + images` format. Without this, router-fed vision turns failed with `json: cannot unmarshal array into Go struct field`.

### Files
- `dragon_voice/llm/router.py` — `CapabilityAwareRouter`, `ModelSpec`, `TIER_FOR_MODE`, `infer_required_caps()`, `summarize()`
- `dragon_voice/llm/base.py` — `Modality` enum + `LLMBackend.capabilities` default
- `dragon_voice/llm/openrouter_llm.py` — `_OPENROUTER_CAPS` + `_PRICING_MILS_PER_M` registries (35 models, last sync 2026-04-27)
- `dragon_voice/messages.py` — multimodal marker encode/decode
- `tests/test_router_routing.py` — 29 routing-rule assertions
- `tests/test_capability_declaration.py` — 31 per-backend cap assertions
- `tests/test_multimodal_persistence.py` — 14 marker + hydration tests

## Local-first Inference on Dragon (LM Studio-compat via llama-server, May 2026)

**Principle: Local mode is Dragon-only.**  Never propose workstation-LAN
inference servers as a Local-first path even when Dragon ARM64 + Ollama
can't run a model — wait for upstream support, or have the user opt
into Cloud / Hybrid explicitly.  Introducing a workstation dependency
breaks both privacy + the "Tab5 + Dragon, nothing else" product story.
(User directive, captured to memory `feedback_dragon_only_local.md`.)

### Why we swapped Ollama → llama-server for the Local LLM path

Same hardware (Q6A ARM64), same `ministral-3:3b` model:

| Path | Direct tool-call probe | End-to-end through Dragon |
|---|---|---|
| Ollama | ~78 s | ~140 s |
| llama-server | **~7.6 s** | **~191 s** (tool exec + LLM wrap) |

The direct-probe ~10× speedup is the load + sampler overhead Ollama
adds; end-to-end is closer because most of the time is consumed by
the same llama.cpp eval underneath.  Net product win: MiniCPM-V family
loads + serves cleanly via llama-server (Ollama 500'd on V-4.6 blobs),
LM Studio-compatible API gives us a richer ecosystem of tooling +
clients, and we're no longer paying Ollama's Go-wrapper overhead.

### Dragon-side wiring

- **Inference server:**  `/home/radxa/llama.cpp/build/bin/llama-server`
  serves OpenAI-compatible `/v1/chat/completions` on `localhost:1234`.
  Built ARM64-native from llama.cpp master (b8696 verified working).
- **Persistent via systemd:**  `tinkerclaw-llama-server.service` (unit
  shipped in `deploy/systemd/`).  Survives reboot, restart-on-failure,
  swap model via the unit's `--model` arg + `systemctl restart`.
- **Dragon LLM backend:**  `dragon_voice/llm/lmstudio_llm.py` —
  un-changed, just point the URL at `localhost:1234`.  Config:
  ```yaml
  llm:
    backend: "lmstudio"          # was "ollama"
    local_backend: "lmstudio"    # was implicit "ollama"
    lmstudio_url: "http://localhost:1234/v1"
    lmstudio_model: "default"    # llama-server reports its loaded GGUF
  ```
- **Token cap:**  `MAX_TOKENS_LOCAL = 1024` (was 128).  Required for
  thinking-mode models (MiniCPM-V-4.6, qwen3-thinking) which spend
  budget inside `<think>...</think>` and emit zero visible content
  when capped low.  Non-thinking models (ministral-3:3b) stop
  naturally well under the cap — no-op cost.
- **HTTP timeouts:**  `lmstudio_llm.py` uses
  `ClientTimeout(total=600, sock_read=300)` — bumped from 120/60
  because Local-mode turns on Q6A routinely hit 90-180 s.

### Default Local LLM: `ministral-3:3b`

Per the 2026-04-25 gauntlet AND the 2026-05-17 llama-server bench:
- Tool-call accuracy: emits clean `<tool>NAME</tool><args>{}</args>`
  markers, fires 7/10 on the gauntlet, 100 % on the new Calendar /
  Gmail / Tasks integration set.
- Latency: ~7-8 s direct probe, ~3 min end-to-end through Dragon's
  full prompt (system + tools + memory + history).
- Reply quality: specific + actionable ("3 standups May 17 at 7 AM,
  1 PM, 4 PM" beats MiniCPM-V's "three scheduled events").
- No thinking tax.

### Backend priority + fallback chain (2026-05-17)

`create_llm()` in `dragon_voice/llm/__init__.py` does a synchronous
TCP probe to `lmstudio_url` when `backend="lmstudio"`.  If the
llama-server socket isn't reachable at process-start, it transparently
falls back to Ollama for the rest of that session.

Implications:
- Restart of `tinkerclaw-llama-server` mid-Dragon-session is NOT
  picked up automatically.  After fixing llama-server, also
  `systemctl restart tinkerclaw-voice` to re-evaluate.
- The fallback is one-shot at instance creation — not per-request —
  so warm-path latency is unaffected.

Order of preference (Local mode):
1. `lmstudio` (llama-server) — when reachable on 127.0.0.1:1234
2. `ollama` — automatic fallback when (1) is down

To force pure Ollama without removing llama-server, set
`llm.backend: "ollama"` + `llm.local_backend: "ollama"` in `config.yaml`.

### Other Local models benchmarked (2026-05-17)

All probed via llama-server on Q6A, same system prompt + tool defs.
Direct probe is "emit a tool call for a single fixed query"; the
end-to-end column is through Dragon's ConversationEngine with full
prompt + memory + tool execution + NL wrap.

| Model | Quant size | Direct | End-to-end | Tool fired | Tool format | Notes |
|---|---|---|---|---|---|---|
| **ministral-3-3b** | 2.1 GB Q4 | 7.6 s | 3 min | ✅ `<tool>NAME</tool>` | Dragon-standard | default |
| MiniCPM-V-4.6 (752M base) | 504 MB Q4 | 3 s | 2.5 min | ✅ `<tool>NAME</tool>` | Dragon-standard | thinking blocks eat tokens, needs MAX_TOKENS_LOCAL≥1024 |
| **Gemma-4-E4B-it** (7.5B) | 5.3 GB Q4 | 33 s | not E2E | ✅ best **accuracy** (picked `calendar_week` for "this week", others picked `_today`) | **`<\|tool_call>call:NAME{}<tool_call\|>` (4th dialect)** | Parser doesn't recognize the format yet; needs registry.py extension before usable through Dragon |
| MiniCPM-V-4.5 (8.2B) | 5.0 GB Q4 | timeout >90 s | — | — | — | too big for Q6A interactive |
| Ministral via Ollama | 2.8 GB | 78 s | 2.5 min | ✅ | Dragon-standard | 10× slower client overhead |

Gemma-4-E4B's parser-mismatch is the cleanest path to upgrading
Local quality: add `<|tool_call>...<tool_call|>` as a 4th dialect in
`dragon_voice/tools/registry.py` (alongside the existing 3 per the
module docstring) and Gemma becomes the new winner — faster than
MiniCPM-V's 3-min turn AND picks the right tool name on the first
shot.  Tracked for a follow-up wave.

### Multimodal (audio / vision) is parked, not skipped

Mainline llama.cpp doesn't yet support MiniCPM-V-4.6's `minicpmv4_6`
projector or MiniCPM-o's audio encoder.  When we need audio/vision
on Dragon:

- **Vision via MiniCPM-V family:**  wait for projector support to land
  upstream, OR rebuild llama.cpp from a feature branch that has it.
- **Audio via MiniCPM-o:**  the realistic path is `tc-mb/llama.cpp-omni`
  (openbmb maintainer's fork) — clean build (~15 min on Q6A), use the
  fork's converter to GGUF-ify MiniCPM-o-4.5 from safetensors.  Tracked
  for a future session.

In the meantime, **Cloud mode (vmode=2)** already handles audio +
vision via OpenRouter — the right answer when the user explicitly
opts in to cloud.

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

## Video Relay + Web Call Client (April 2026)

Tab5 ↔ Dragon ↔ (Tab5 or web) two-way video + audio calls. Dragon is a dumb fan-out relay — no transcoding.

- **Module:** `dragon_voice/video_upstream.py` — receives `VID0`-tagged binary frames from one connected Tab5 and **broadcasts verbatim to all OTHER connected clients**. Same fan-out applies to `AUD0`-tagged frames in the call audio path. No re-encode, no buffering beyond the WS write queue.
- **Wire format (see `docs/protocol.md` §18):** `"VID0"` (4 bytes) + `len_be` (4 bytes BE u32) + JPEG payload. `"AUD0"` (4 bytes) + `len_be` + raw 16 kHz mono int16 PCM. Untagged binary frames are still mic-PCM bound for STT.
- **`POST /api/video/inject`** — debug endpoint that pushes a JPEG into the relay as if it had come from a Tab5; useful for testing the Tab5 downlink decode path without a second device. Bearer-auth.
- **`/call`** — serves `dragon_voice/static/call.html`, a minimal browser call client (HTML + JS, `getUserMedia` for camera + mic, Web Audio API for 16 kHz PCM capture and playback). The web client is just another participant on the relay — Dragon doesn't know or care it's a browser. Shipped in #180 (video) + #181 (audio).
- **Audio in calls:** Web client captures 16 kHz mono PCM via Web Audio, wraps with `AUD0`, and plays inbound `AUD0` frames back through an `AudioBufferSourceNode` chain.

## OPUS Audio Codec (April 2026 — partial)

- **Capability negotiation works end-to-end** (TinkerBox #174 + TinkerTab #263/#265): Tab5's `register` frame advertises codec capabilities; Dragon responds in `session_start.config` with the negotiated codec.
- **Decoder ready** for Phase 2B Dragon→Tab5 OPUS TTS — Tab5 can decode OPUS frames if Dragon emits them.
- **Encoder BROKEN on Tab5** — SILK NSQ crash on ESP32-P4 mid-encode. Encoder is gated OFF in TinkerTab `voice_codec.h` pending TinkerTab #264 root-cause. Don't enable the OPUS uplink path until #264 closes.

## Channel Messaging — W7-E (Tab5 surface) + W7-F (Dragon connector) (May 2026)

The channel-messaging surface lets a third-party messaging platform (Telegram,
WhatsApp, Discord, Slack, Signal, iMessage, Matrix, Email) deliver a message to
Tab5 via Dragon, and lets the user reply back through the same path.  W7-E owns
the Tab5 UI (toast / now-card / dedupe / snooze / quiet-hours / voice-dictated
reply).  W7-F owns the Dragon side (WS dispatcher → channel_reply_ack +
GatewayConnector → OpenClaw channel plugins).

### Wire frames
Full schema lives in [`docs/protocol.md`](docs/protocol.md) §20.  Three frame
types: `channel_message` (Dragon → Tab5), `channel_reply` (Tab5 → Dragon),
`channel_reply_ack` (Dragon → Tab5).

### Dragon-side modules (W7-F)
- `dragon_voice/channel_reply_handler.py` — WS dispatcher entry for the
  `channel_reply` cmd_type.  Logs the receive, builds the ACK frame, calls
  the GatewayConnector to forward to OpenClaw.
- `dragon_voice/channels/__init__.py` — package boundary.
- `dragon_voice/channels/gateway.py` — `GatewayConnector`, the WS-RPC client
  for OpenClaw's `localhost:18789` gateway.  Signed-connect handshake using
  ed25519 device identity (W7-F.4), connect.challenge nonce flow, role +
  scopes (operator + operator.read + operator.write + operator.admin),
  PROTOCOL_VERSION 3.  `_TAB5_CHANNEL_ALIASES` dict maps Tab5 short codes
  (`tg`, `wa`, `dc`, `sl`, `sg`, `im`, `ma`, `em`) → OpenClaw canonical
  plugin names (`telegram`, `whatsapp`, …) — W7-F.5.
- `dragon_voice/channels/device_identity.py` — ed25519 keypair persistence
  at `~/.dragon/identity/device.json` (0o600, atomic write, regenerates on
  corruption).  Mirrors `openclaw/src/infra/device-identity.ts` byte-for-byte
  including `buildDeviceAuthPayloadV3`.  Loopback + gateway-client + backend
  + token-auth satisfies `shouldSkipBackendSelfPairing` so no manual pairing
  approval is needed for a fresh keypair.
- `dragon_voice/channels/mock.py` — `MockConnector` for tests (in-process
  fake that records calls + lets the suite assert connector behavior without
  a running OpenClaw gateway).
- `dragon_voice/channels/base.py` — abstract `ChannelConnector` interface
  (`send_reply`, lifecycle hooks).
- `dragon_voice/api/debug_channel.py` — `POST /api/v1/debug/channel_message`
  REST endpoint that fans a synthetic frame to a connected Tab5 over the
  existing voice WS.  Mirrors `api/video_inject.py`'s structure.  Used for
  Tab5-side notification-surface testing without a real OpenClaw channel
  plugin loaded.  Records the push as a `channel_push` source entry in
  `agent_log` so the Tab5 Agents overlay's source counts pick it up.

### Status (2026-05-13)
The W7-F connector chain is **fully end-to-end functional**: handshake
succeeds, scopes are granted, channel-name alias maps correctly, and
`gateway.send` reaches the real platform API.  W7-F.5's live verification
showed `Error: Telegram send failed: chat not found (chat_id=42)` —
expected because no real Telegram chat is paired.  A real paired bot will
round-trip to `ok=true platform_message_id=<real-tg-id>`.

### Live test surface
- `POST /api/v1/debug/channel_message?device_id=<MAC>` with body
  `{"channel":"tg","message_id":"…","sender":{"display_name":"…","starred":true},
   "preview":"…","priority":"high","needs_reply":true}` → Tab5 surfaces the
  message per its W7-E routing.
- Tab5's REPLY-button flow (W7-E.4b real voice-dictated) sends `channel_reply`
  via the existing voice WS → `channel_reply_handler.handle_channel_reply`
  → `GatewayConnector.send_reply` → OpenClaw plugin → platform API → ACK
  trace ends with Tab5 obs `ui.notif.reply ack_ok`.

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

## Local LLM Benchmarks — see CHANGELOG

The 10-prompt gauntlet table (2026-04-25, post-#74/#76/#77) ranking
ministral-3:3b / gemma3:4b / xLAM / qwen3:* / phi4-mini / llama3.2:3b /
etc., the three failure classes, and the "when to use what" matrix
have moved to [`docs/CHANGELOG.md`](docs/CHANGELOG.md) — they're
dated reference, not runbook.  Quick summary:

- **Current default:** `ministral-3:3b` (configured in
  `dragon_voice/config.yaml`).  7/10 correct-tool fires, 5/10 visible
  replies, math correct on the 456×789 gauntlet prompt.
- **First alternative:** `gemma3:4b` — best visible-reply rate (7/10),
  ~12 s slower per turn, 1 GB more RAM.
- **Cloud / TinkerClaw modes are still better for long agentic chains;
  Local is usable post-#74/#76/#77 but not best on accuracy or speed.**

## Current Sprint — see CHANGELOG

Phase 0 (Foundation) and Phase 1 (Voice Features) are complete; the
agentic sprint (tool-calling, memory, documents) is done.  Dragon is a
fully functional API-first voice assistant server with agentic
capabilities.

The completed-issues table + schema summary + initial acceptance-test
list have moved to [`docs/CHANGELOG.md`](docs/CHANGELOG.md).
Architecture decisions (Session ≠ Connection, append-only messages,
etc.) stay below as durable design rules.

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
See `schema.sql` — **11 tables**: 6 foundation (devices, sessions, messages, notes, events, config), 3 memory (memory_facts, memory_documents, memory_chunks), and 2 scheduler (scheduled_notifications, notification_queue).

## API-First Architecture (58 REST endpoints + 1 WebSocket)

_Counted 2026-05-13 via:
`grep -c 'app.router.add_' dragon_voice/api/*.py dragon_voice/notes/api.py | awk -F: '{s+=$2} END {print s}'` → 58.
W7 sprint added 4 endpoints: `/api/v1/spend` (W5-A cross-stack audit),
`/api/v1/agent_skills` (W7-B catalog), `/api/v1/debug/channel_message` (W7-F stub
push), `/api/video/inject` (#178 debug injector — pre-W7 but missed in prior count).
Prior count was 54 (post-#187 multi-model router); 53 was the count before #178;
52 before TT #328 Wave 12 added `/api/v1/agent_log`; 47 before Phase 5 ε1b added
the 5 scheduler endpoints._

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
| **Agent log** | GET | `/api/v1/agent_log` | Cross-session tool-call activity feed (last 64; populated at `ToolRegistry.execute` chokepoint).  TT #328 Wave 12. |
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
| **Scheduler** | POST | `/api/v1/scheduler/notifications` | Schedule a notification (when, message, device_id) |
| | GET | `/api/v1/scheduler/notifications` | List notifications (filter by device_id, status) |
| | GET | `/api/v1/scheduler/notifications/{id}` | Get one notification |
| | DELETE | `/api/v1/scheduler/notifications/{id}` | Cancel a pending notification |
| | PATCH | `/api/v1/scheduler/notifications/{id}` | Reschedule (change `when`) |
| **Spend** | GET | `/api/v1/spend?day=YYYY-MM-DD` | W5-A: daily LLM spend roll-up over the `events` table.  Empty `day` = today UTC.  Backed by `dragon_voice/billing/spend_tracker.py`. |
| **Agent skills** | GET | `/api/v1/agent_skills` | W7-B: merged catalog of OpenClaw core tools (static 8) + tool names observed in `agent_log`.  Tab5 fetches on Agents-overlay open + on voice-mode change (when overlay visible). |
| **Channel push (debug)** | POST | `/api/v1/debug/channel_message?device_id=X` | W7-F stub: fan a synthetic `channel_message` JSON frame to a connected Tab5 over the existing voice WS.  Mirrors `video_inject` shape.  Real gateway-driven push lives in W7-F.2's `GatewayConnector` (`dragon_voice/channels/gateway.py`). |
| **Video (debug)** | POST | `/api/video/inject?device_id=X` | #178: push a JPEG frame as if from a paired Tab5 — exercises the downlink decode + ui_video_pane render path without a second device.  Body = raw JPEG; wraps with VID0 magic + 4-byte BE length. |

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
`server.py` itself (was 1,803 LOC right after the decomposition; has
since regrown to ~2,720 LOC as Phase 1-3 UX-gap fixes + the multi-
model router + Wave 12 agent_log instrumentation accreted to the
core WS handler family).  The extracted modules take deps explicitly
(server handle or specific args), so each can be unit-tested without
instantiating the full server.

```
schema.sql            — Database schema (11 tables: 6 foundation + 3 memory + 2 scheduler)
dashboard.py          — Web dashboard (port 3500, aggregates device state + voice state)
dragon_voice/         — Voice pipeline package (port 3502)
  __init__.py         — Package init
  __main__.py         — Entry point: python3 -m dragon_voice
  server.py           — VoiceServer class + create_app wiring + WS-voice handler family
                        (register / text / user_media / disconnect / audio + event hooks).
                        ~2,720 LOC (was 1,803 right after #65 decomposition; regrown as
                        Phase 1-3 UX-gap fixes + multi-model router + W7-A/B/F handlers
                        accreted).  Top-level `dragon_voice/` now has 60+ extracted
                        sibling modules — see the *_handler.py / *_swap.py / config_*.py /
                        *_path.py / *_emit.py families directly under the package root.
                        Find them with `ls dragon_voice/*.py | wc -l`.  Each sibling owns
                        one concern (config swap guards, config update rate limit,
                        widget action handler, channel reply handler, vision turn,
                        local text stream, tinkerclaw text path, etc.) so tests can
                        import them in isolation without spinning up `VoiceServer`.
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
  api/                — Modular REST API package (58 endpoints as of 2026-05-13)
    __init__.py       — setup_all_routes() entry point — wires all route classes
    utils.py          — Shared helpers (json_error, pagination)
    sessions.py       — Session CRUD + lifecycle routes
    messages.py       — Message listing + SSE chat routes
    devices.py        — Device CRUD routes
    config_routes.py  — Config CRUD + delete routes
    events.py         — Events listing with device_id filter
    agent_log.py      — Cross-session tool-call ring buffer + GET /api/v1/agent_log
                        (TT #328 Wave 12).  W7-A.3 added `source` field
                        (dragon/gateway/channel_push/user_reply) so consumers can
                        bucket activity.  Populated at ToolRegistry.execute
                        chokepoint + by `debug_channel.py` + `channel_reply_handler.py`.
    agent_skills.py   — W7-B catalog: GET /api/v1/agent_skills.  Merges static list
                        of 8 OpenClaw core tools (bash, browser, edit_file, memory,
                        read_file, search_files, task, web_search) with tool names
                        observed in agent_log.  Tab5 fetches on Agents-overlay open
                        + on /mode POST when overlay is visible (W7-B + W7-B.4 +
                        TT #467 follow-up).
    debug_channel.py  — W7-F stub: POST /api/v1/debug/channel_message.  Fans
                        synthetic channel_message frames to a connected Tab5 over
                        the existing voice WS.  Records the push as `channel_push`
                        agent_log entry.
    video_inject.py   — #178 debug-only POST /api/video/inject.  Push a JPEG as if
                        from a paired Tab5; exercises downlink decode + ui_video_pane
                        without a second device.  Body = raw JPEG, wraps with VID0
                        magic + 4-byte BE length.
    spend.py          — W5-A: GET /api/v1/spend[?day=YYYY-MM-DD].  Daily LLM spend
                        roll-up over the events table.  Backed by
                        `dragon_voice/billing/spend_tracker.py`.
    scheduler.py      — 5 endpoints for the in-process notification scheduler
                        (RFC-scheduler.md; ε2 store at sqlite for replay).
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
    registry.py       — ToolRegistry: register, parse XML markers, execute.
                        TT #328 Wave 12: execute() also feeds the agent_log
                        ring buffer for the /api/v1/agent_log feed.  Single
                        canonical instrumentation site so REST + WS + dashboard
                        callers all surface in the same activity log.
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
  llm/                — LLM backends + multi-model router (#183-#187)
    base.py             — LLMBackend ABC + Modality enum + capabilities default
    router.py           — CapabilityAwareRouter, ModelSpec, TIER_FOR_MODE,
                          infer_required_caps, summarize.  Opt-in via
                          backend: "router" + a populated fleet[].
    ollama_llm.py       — Ollama (multimodal-aware translator added in #186)
    openrouter_llm.py   — OpenRouter + _OPENROUTER_CAPS (35 models, sync 2026-04-27)
                          + _PRICING_MILS_PER_M (matched 1:1 with caps registry)
    lmstudio_llm.py     — Local OpenAI-compatible server (LAN tier in fleet)
    npu_genie.py        — QAIRT/Genie (text-only, ~8 tok/s on QCS6490 HTP)
    tinkerclaw_llm.py   — TinkerClaw gateway adapter (voice_mode=3)
    dual.py             — Two-backend picker+responder (predates router)
  notes/              — Notes module (CRUD + search + audio ingestion)
  media/              — Rich media rendering package
    __init__.py       — Package exports (MediaStore, MediaPipeline)
    store.py          — MediaStore: disk-backed media file storage, 24h auto-cleanup, 500MB max
    pipeline.py       — MediaPipeline: detects code/tables/image URLs in LLM output, renders JPEG via Pygments/Pillow
    url_signer.py     — MediaUrlSigner: HMAC-signed + time-bounded /api/media/{id} URLs (W14-H04)
  scheduler/          — Phase 5 async push: in-process scheduler + sqlite-backed
                        notification store.  See docs/RFC-scheduler.md.
    manager.py        — SchedulerManager (RUNAWAY_CAP_PER_DEVICE guards) + REST glue
    models.py         — Notification dataclass
    parser.py         — `parse_when` natural-language time parser
    store.py          — InMemoryNotificationStore + SqliteNotificationStore
                        (boot replay + offline queue + snooze; ε2 PR #132)
  surfaces/           — Tab5 widget-surface abstraction (widget_live/card/list/chart/media/prompt)
  channels/           — W7-F third-party messaging channel infrastructure
    __init__.py       — Package exports
    base.py           — `ChannelConnector` abstract base (`send_reply`, lifecycle)
    gateway.py        — `GatewayConnector` WS-RPC client to OpenClaw `localhost:18789`.
                        Signed-connect handshake (ed25519 device identity, connect.challenge
                        nonce, role=operator + operator.read/write/admin scopes,
                        PROTOCOL_VERSION 3).  `_TAB5_CHANNEL_ALIASES` maps Tab5 short codes
                        (tg/wa/dc/…) → OpenClaw canonical plugin names (W7-F.5).
    device_identity.py — ed25519 keypair persistence at `~/.dragon/identity/device.json`
                        (0o600, atomic write, regenerates on corruption).  Mirrors
                        `openclaw/src/infra/device-identity.ts` byte-for-byte including
                        `buildDeviceAuthPayloadV3` canonical-string serialization.
    mock.py           — In-process `MockConnector` for tests — records `send_reply` calls
                        + canned ACK shapes, no real gateway needed.
  channel_reply_handler.py — WS dispatcher entry (W7-F stub).  Handles `cmd_type ==
                        "channel_reply"` frames: logs receive, forwards to the active
                        `ChannelConnector`, emits `channel_reply_ack` JSON back to Tab5.
                        Source-flag agent_log entries as `user_reply`.
  mcp/                — Model Context Protocol client + bridge
tests/                — Test suite (**1533 tests collected** as of 2026-05-13 post-W7
                        sprint; +977 since the 2026-04-28 baseline thanks to the W7-A
                        agent_log instrumentation, W7-B agent_skills, W7-F gateway
                        connector + device-identity + channel-alias suites, W5-A spend
                        tracker, plus accumulated middleware/router/handler-extraction
                        coverage).  Verify with
                        `python3 -m pytest tests/ --collect-only -q --ignore=tests/audit`.
                        The test_api_e2e.py CLI runner contributes another 29 live-Dragon
                        scenarios that don't run in CI.
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
  router-cookbook.md  — Multi-model router fleet recipes (#188)
  npu-setup.md        — Qualcomm NPU / QAIRT SDK setup guide
  AUDIT-WAVE-15.md    — Current wave audit (Wave 14 archived; see below)
  WAVE-15-PROGRESS.md — Per-item checklist for the active Wave 15 sprint
  PLAN-dual-model-pipeline.md — Dual-model pipeline plan + post-mortem
  RFC-scheduler.md    — Scheduler/notifications subsystem design
  SKILL_AUTHORING.md  — Skill SDK reference (uses tools/quick_poll_tool.py as example)
  SOLID-AUDIT.md      — SOLID/structural audit of both repos
  UX-GAPS.md          — Master UX gap tracker (issue #89)
  telegram-bot.md     — Telegram bot deployment guide
  historical/         — Closed waves + superseded audits
    README.md         — Index of archived docs + why each was moved
    AUDIT-WAVE-14.md  — Wave 14 audit (closed 2026-04-21)
    WAVE-14-PROGRESS.md — Wave 14 per-item checklist (all items shipped)
LEARNINGS.md          — Institutional knowledge (MANDATORY reading)
```

Note that file:line citations in any audit doc that pre-dates 2026-04-24
target the pre-#65 monolithic `server.py`; many of those positions now
live under `dragon_voice/middleware/`, `dragon_voice/handlers/`, or
`dragon_voice/lifecycle/`.

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

Aggregate pytest collection (including `tests/audit/`): **1533 tests collected** as of 2026-05-13 (post-W7 sprint).  Verify with `python3 -m pytest tests/ --collect-only -q`.  The CI named-set is a tighter subset of files picked so each can run without a live server; everything else is local-only.  Recent additions: W7-F gateway connector suite (~69 tests across `tests/test_channel_*.py` + `tests/test_gateway_*.py`), W7-A `agent_log` source-field tests, W5-A spend-tracker tests.

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
# From workstation (Dragon must be running on 192.168.70.242:3502 — see Dragon Access for current IP)
python3 tests/test_api_e2e.py
```
