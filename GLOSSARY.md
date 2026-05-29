# GLOSSARY — Tinker stack canonical terms

> **Part of the Tinker stack** — four repos, each documented on its own:
> [**TinkerTab**](https://github.com/lorcan35/TinkerTab) (Tab5 device firmware) ·
> [**TinkerBox**](https://github.com/lorcan35/TinkerBox) (Dragon inference server — "the brain") ·
> [**PingOS**](https://github.com/lorcan35/PingOS) (portable Tab5 OS) ·
> [**TinkerClaw**](https://github.com/lorcan35/TinkerClaw) (agent sidecar).
> New here? Each repo's README is its own front door.

This is the canonical, cross-stack glossary. Every repo carries the **full
canonical core** below (the repos stand alone), plus an optional
`## <Repo>-specific terms` section for terms that only matter inside that repo.
Each entry is a one-line definition; some add a "see" pointer to the page that
owns the deeper treatment.

## Core — the stack

- **Tinker stack** — the four-repo product: Tab5 firmware (TinkerTab), Dragon server (TinkerBox), portable OS (PingOS), agent sidecar (TinkerClaw). The live product is Tab5 + Dragon talking over one WebSocket.
- **Tab5** — the M5Stack Tab5 hardware and its firmware: ESP32-P4 + 5" 720×1280 IPS display + MIPI-CSI camera + 4-mic array + ES8388 DAC + Wi-Fi via a hosted ESP32-C6 + 16 MB flash + 32 MB PSRAM + 6 Ah LiPo + capacitive touch. The **face** of the stack — a thin client that owns the UI, sensors, and audio but no intelligence.
- **ESP32-P4** — the dual-core RISC-V SoC inside the Tab5. Runs the firmware bare-metal under FreeRTOS + ESP-IDF; has ~512 KB internal SRAM (tight, fragments) backed by 32 MB PSRAM for large buffers. No native Wi-Fi — Wi-Fi is provided by a hosted ESP32-C6.
- **Dragon (Radxa Q6A)** — the **brain** of the stack. A Radxa Dragon Q6A SBC (Qualcomm QCS6490, ARM64) running Ubuntu and the `dragon_voice` Python service on port 3502. Hosts STT, LLM (router-fronted), TTS, embeddings, the conversation engine, tools, memory, the dashboard, and OTA. See TinkerBox `docs/ARCHITECTURE.md`.
- **TinkerBox** — the repo and Python service that runs on the Dragon. Owns all intelligence: STT, LLM, TTS, sessions, conversation engine, REST API, dashboard, database.
- **TinkerTab** — the repo holding the Tab5's C/ESP-IDF firmware. Thin client: LVGL UI, mic/speaker/camera/touch, SD card, Wi-Fi, NVS settings. Sends audio/text, receives STT/LLM/TTS results over the WebSocket.
- **PingOS** — the portable Tab5 OS: a BSP-abstracted, DPI-scaling, portable LVGL layer derived from the Tab5 UI so the interface can run on hardware beyond the M5Stack Tab5.
- **TinkerClaw** — the optional agent sidecar (OpenClaw-derived). Activated by voice mode 3; runs its own LLM choice + tool execution + browser automation on the Dragon at `localhost:18789`. Dragon becomes an audio pipe in this mode.

## Core — the K144 / TinkerON module

- **K144 / TinkerON** — the M5Stack LLM Module Kit (AX630C NPU). **"TinkerON"** is the user-facing brand name; **K144 / AX630C / sherpa-ncnn** are the canonical hardware identifiers used in technical docs, logs, and code symbols. Provides on-device ASR + LLM + TTS over the M5-Bus so the Tab5 can run voice turns with no Dragon present (voice mode 4) and always-on wakeword.
- **StackFlow** — the K144's on-device service daemon and its newline-delimited JSON wire protocol over the M5-Bus UART. Setup returns a `work_id` (e.g. `llm.1000`); inference streams `{"object":"llm.utf-8.stream","data":{"delta":...,"finish":bool}}`. Verified `sys.*` verbs: `sys.ping`, `sys.hwinfo`, `sys.lsmode`, `sys.reset`, `sys.reboot`, `sys.version`.

## Core — voice modes

- **Voice mode 0 — Local** — STT (Moonshine), LLM (Dragon-local via llama-server / Ollama / NPU), TTS (Piper), all on the Dragon. Privacy-first; slow on Q6A (~60–90 s/turn). The default.
- **Voice mode 1 — Hybrid** — STT and TTS go to the cloud (OpenRouter `gpt-audio-mini`); the LLM stays Dragon-local. Best dollar-per-turn for a snappy assistant.
- **Voice mode 2 — Cloud** — STT, LLM, and TTS all go through OpenRouter. 3–6 s/turn typical; the LLM is router-picked or user-selected.
- **Voice mode 3 — TinkerClaw** — the TinkerClaw gateway runs the turn; Dragon is an audio pipe (STT + TTS local, ConversationEngine/tools/memory bypassed).
- **Voice mode 4 — Onboard (K144)** — the K144/TinkerON module runs the whole LLM turn on-device; no Dragon needed. Tab5-side-only mode.
- **Voice mode 5 — Solo** — the Tab5 talks straight to OpenRouter for STT/LLM/TTS with no Dragon, using the `or_*` NVS keys + on-device RAG against `/sdcard/rag.bin`. Tab5-side-only mode.

## Core — wakeword + audio path

- **Wakeword** — the always-on "Hey Tinker" listener. The K144's ASR streams partial transcripts; a lenient multi-pattern matcher (accepts `tinker`/`thinker`/`hicker`/`hick`/`hanker`) fires a voice turn on a match. Open-vocabulary — change one string, no model retraining.
- **`ext_pcm`** — the external-PCM path: the Tab5's own mic is pumped over the M5-Bus to the K144's ASR (instead of the K144's own mic), so wakeword runs on the Tab5's better mic array. Pump pauses during a live voice turn so the Tab5 mic routes to the Dragon.
- **`wake_src`** — the configuration value selecting where wakeword audio comes from — the K144's own mic, or the Tab5 mic via `ext_pcm`.

## Core — LLM serving + models

- **Native tool-calling (`native_tools`)** — using the inference server's structured `tools=[...]` API (and `tool_choice`) instead of prose-listing tools in the system prompt, so the model gets a structured signal for which function to call. Contrast with the XML-marker dialects parsed from free-text output.
- **llama-server** — the OpenAI-compatible inference server from llama.cpp, built ARM64-native on the Dragon, serving `/v1/chat/completions` on `localhost:1234`. Replaced Ollama for the Local LLM path (~10× faster direct-probe; no Go-wrapper overhead). Run as `tinkerclaw-llama-server.service`.
- **Granite** — IBM Granite 4.0 Nano (1B) — the winning model for native K144 local-mode tool-calling (19–20/20 on the gauntlet). The candidate to become the live Local default.
- **Moonshine** — the STT model on the Dragon CPU (`medium-streaming-en` quantized). ~400–800 ms for a 2-second utterance on the Q6A.
- **Piper** — the default Dragon TTS engine (`en_US-lessac-medium`). Outputs 22 050 Hz mono int16 PCM, resampled to 16 kHz before sending to the Tab5. **Kokoro** is the alternative higher-quality Dragon TTS engine.

## Core — the WebSocket protocol

The Tab5 holds exactly one persistent WebSocket to the Dragon at `ws://<host>:3502/ws/voice`; all traffic multiplexes over it. See TinkerBox `docs/protocol.md` for the full contract.

- **`register`** — Tab5 → Dragon. First frame on connect: `{"type":"register","device_id":"...","session_id":"..."}`. Identifies the device + resumes a session.
- **`config_update`** — Tab5 → Dragon. Swaps `voice_mode` and/or `llm_model` mid-session: `{"type":"config_update","voice_mode":0|1|2|3,"llm_model":"..."}`. Modes 4 and 5 are Tab5-side-only and downconvert to 0 on the wire. Dragon ACKs with the applied config (incl. `fleet_summary`).
- **`text`** — Tab5 → Dragon. A typed turn: `{"type":"text","content":"..."}` — skips STT, goes straight to the LLM with the same conversation context.
- **`user_media`** — Tab5 → Dragon. Announces a previously-uploaded image (by `media_id`) so the Dragon runs a vision turn. (`user_image` is the same announcement under its older name.)
- **`media`** — Dragon → Tab5. An inline rendered image bubble: `{"type":"media","media_type":"image","url":"...","width":...,"height":...,"alt":"..."}`. Dragon renders code blocks/tables to JPEG and threads them inline.
- **`text_update`** — Dragon → Tab5. Replaces the last AI bubble's text: `{"type":"text_update","text":"cleaned text"}`. Used after the Dragon strips rendered code blocks it sent as `media`.
- **`channel_message`** — Dragon → Tab5. An incoming third-party platform message (Telegram, WhatsApp, Discord, Slack, Signal, iMessage, Matrix, Email): `{"type":"channel_message","channel":"tg","message_id":"...","sender":{...},"text":"...","priority":"...","needs_reply":bool}`. Routed by the Tab5 to a toast or a now-card.

## Core — UI + framework

- **Diátaxis** — the documentation framework these docs follow ([diataxis.fr](https://diataxis.fr)). Four content types, each with one job, never mixed: **tutorial** (learning by doing), **how-to** (achieve a task), **reference** (look up facts), **explanation** (understand why). See [`STYLE.md`](STYLE.md).
- **LVGL** — Light and Versatile Graphics Library v9.2.2 — the native UI framework. Every Tab5 screen/widget/overlay is LVGL. Critical config lives in `sdkconfig.defaults` (not `lv_conf.h`, which is skipped). `lv_async_call` is NOT thread-safe — always use `tab5_lv_async_call`.
- **NVS** — Non-Volatile Storage: the ESP-IDF flash key-value store holding the Tab5's settings (namespace `"settings"`, max key length 15 chars). See TinkerTab `CLAUDE.md` "NVS Settings Keys" for the canonical table.
- **WS** — WebSocket. The single persistent channel between Tab5 and Dragon (`/ws/voice`, port 3502) over which all voice/text/vision/video/config/event traffic multiplexes.

## TinkerBox-specific terms

Terms that only matter inside the Dragon server (TinkerBox). The canonical core
above is shared across all four repos; these are the brain's internals.

- **Backend** — a concrete `LLMBackend` subclass that talks to one inference provider: `ollama`, `openrouter`, `lmstudio`, `npu_genie`, `tinkerclaw`, `dual`, or the `router`. Each declares its `capabilities` (a `frozenset[Modality]`) and implements `generate_stream_with_messages(messages)`. Selected by `LLMConfig.backend` in `config.yaml`. See `dragon_voice/llm/base.py`.
- **Backend pool** — Dragon's process-wide cache of warm `LLMBackend` instances keyed by config signature (`pipeline._llm_sig`), so a per-connection `config_update` doesn't reload an Ollama/llama-server model from scratch. Owned by `VoiceServer._backend_pool`.
- **`CapabilityAwareRouter` / router fleet** — the multi-model router (`dragon_voice/llm/router.py`). Implements `LLMBackend` so it slots into the ConversationEngine like any single model, but holds N sub-backends and picks one per turn from the inferred required capabilities + the tier policy for the active voice mode. Opt-in: `backend: "router"` + a populated `fleet`. See `docs/router-cookbook.md`.
- **Fleet** — `LLMConfig.fleet: list[ModelSpec]` in `config.py` — the set of models the router can choose among. Each `ModelSpec` declares `id`, `backend`, `model_id`, `caps`, `tier` (`local`/`cloud`/`lan`), `priority` (lowest wins), `keep_alive_s`. Empty list + non-`router` backend = legacy single-backend dispatch.
- **Tier** — a model's deployment cohort: `local` (on Dragon), `cloud` (OpenRouter), or `lan` (e.g. LM Studio on a workstation). `TIER_FOR_MODE` maps each voice mode to the set of tiers the router will pick from (mode 0/1 → local, mode 2 → cloud + lan, mode 3 → router bypassed).
- **`lmstudio`** — the OpenAI-compatible local backend (`dragon_voice/llm/lmstudio_llm.py`). Points at `localhost:1234`, where the Dragon's ARM64-native llama-server serves `/v1/chat/completions` (the `tinkerclaw-llama-server.service` unit). This is the preferred Local LLM path (≈10× faster direct-probe than Ollama); `create_llm()` falls back to Ollama if the llama-server socket isn't reachable at process start.
- **ConversationEngine** — `dragon_voice/conversation.py`. The orchestrator for a multi-turn LLM conversation with memory-augmented context + tool-calling + history. Entry point `process_text_stream(session_id, text, media_id=None, ...)`. Voice, text, and vision turns all converge here. Bypassed only in voice mode 3 (the TinkerClaw gateway runs the turn).
- **MediaPipeline** — `dragon_voice/media/pipeline.py`. After an LLM turn finishes, scans the response for code blocks / markdown tables / image URLs and renders each as a JPEG (Pygments for code, Pillow for tables), up to 3 per response; then strips the rendered content from the text via a `text_update` frame. Rendered blobs live in the **MediaStore** (`media/store.py`: 24 h TTL, 500 MB cap, `/home/radxa/media/`).
- **Scheduler** — the async-push subsystem (`dragon_voice/scheduler/`): an in-process `SchedulerManager` + a SQLite-backed notification store (`SqliteNotificationStore`) for time-deferred reminders delivered over the voice WS at fire time and replayable across reboots. Natural-language times via `parse_when`. Design: `docs/internal/RFC-scheduler.md`.
- **Channels / gateway** — the third-party-messaging layer (`dragon_voice/channels/`). `GatewayConnector` (`channels/gateway.py`) is the WS-RPC client to the OpenClaw gateway at `localhost:18789`, with an ed25519 signed-connect handshake (`channels/device_identity.py`) and a Tab5-short-code → OpenClaw-plugin-name alias map (`tg`→`telegram`, etc.). Carries `channel_message` (Dragon→Tab5), `channel_reply` (Tab5→Dragon, via `channel_reply_handler.py`), and `channel_reply_ack` frames.
- **`agent_log`** — the cross-session tool-call activity feed (`dragon_voice/api/agent_log.py`, `GET /api/v1/agent_log`). A 64-entry ring populated at the single `ToolRegistry.execute` chokepoint, with a `source` field (`dragon` / `gateway` / `channel_push` / `user_reply`) so consumers can bucket activity. Surfaced on Tab5's Agents overlay.

## Cross-references

- **System architecture (the map):** [TinkerBox `docs/ARCHITECTURE.md`](https://github.com/lorcan35/TinkerBox/blob/main/docs/ARCHITECTURE.md)
- **Wire protocol:** [TinkerBox `docs/protocol.md`](https://github.com/lorcan35/TinkerBox/blob/main/docs/protocol.md)
- **Documentation writing standard:** [`STYLE.md`](STYLE.md)
- **How the four repos relate:** `docs/explanation/how-the-stack-fits-together.md` (in TinkerTab and TinkerBox)
