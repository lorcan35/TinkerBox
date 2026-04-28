# TinkerClaw Glossary

> Single-line definitions for terms that appear across both repos
> (TinkerBox = Dragon brain, TinkerTab = Tab5 firmware).  When a term
> means something different in another context, the link points at the
> code or doc where the canonical definition lives.

## A

**`add_message`** — `MessageStore.add_message(session_id, role, content, …)` in [`messages.py`](dragon_voice/messages.py). Appends one message to the session log. Optional `media_id` parameter encodes a multimodal user message via the `__mm__:` content marker. Append-only; messages never mutate.

**Agentic pipeline** — Dragon's tool-calling loop. LLM emits `<tool>NAME</tool><args>{...}</args>` markers; [`tools/registry.py`](dragon_voice/tools/registry.py) parses them, executes the matching `Tool`, injects the result back into the conversation, and re-prompts the LLM. Capped at `MAX_TOOL_CALLS=3` per turn.

**`AUD0`** — 4-byte ASCII magic prefix on a binary WS frame indicating it's call audio (raw 16 kHz mono int16 PCM), not mic-PCM-bound-for-STT. See [`flows/video-call.md`](docs/flows/video-call.md).

**`auth_tok`** — Tab5 NVS key holding the 32-char hex bearer token for the Tab5 firmware's debug HTTP server (port 8080). Auto-generated on first boot, persists across reboots. Different from `DRAGON_API_TOKEN`, which gates Dragon's `/api/*` REST endpoints.

## B

**Backend pool** — Dragon's process-wide cache of warm `LLMBackend` instances keyed by config signature (see `pipeline._llm_sig`). Avoids re-loading Ollama models on every connection's `config_update`. Owned by `VoiceServer._backend_pool`.

**β-arch (beta-arch)** — issue [#123](https://github.com/lorcan35/TinkerBox/issues/123). Unified `progress` event bus that supersedes per-phase ad-hoc events (`tool_call`, `dictation_postprocessing`, etc.). Migration is incremental — `progress_bus_emit_legacy: true` keeps double-emitting for backwards-compat with current Tab5.

## C

**Capability** / **`Modality`** — One of `TEXT`, `VISION`, `VIDEO`, `AUDIO_IN`, `AUDIO_OUT`, `TOOL_CALLING`. Each `LLMBackend` declares a `frozenset[Modality]` via the `capabilities` property; the router uses these to pick a backend per turn. See [`llm/base.py`](dragon_voice/llm/base.py) and [`flows/vision-turn.md`](docs/flows/vision-turn.md).

**`CapabilityAwareRouter`** — The router itself. Implements `LLMBackend` so it slots into ConvEngine like any single-model backend. Holds N sub-backends, picks per call based on inferred caps + tier policy. Lazy-instantiates sub-backends. See [`llm/router.py`](dragon_voice/llm/router.py) and [`docs/router-cookbook.md`](docs/router-cookbook.md).

**`cap_mils`** — Tab5 NVS key holding daily LLM-spend cap in mils (1/1000 ¢). Default 100000 (= $1.00/day). When `spent_mils` exceeds this on a Cloud-mode turn, Tab5 auto-downgrades to Local mode and announces `cap_downgrade` via `config_update`.

**`cam_rot`** — Tab5 NVS key (uint8 0..3) holding camera rotation in 90° steps (0=none, 1=90°CW, 2=180°, 3=270°CW). Applied in software after V4L2 capture before display, photo save, and video record. Settings dropdown + an in-viewfinder "Rot" button writes the key.

**Cloud mode** — `voice_mode = 2`. STT, LLM, TTS all go through OpenRouter (LLM is router-picked). 3-6 s/turn typical. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) "The four modes."

**Compact tool format** — When the active LLM is a small local model (e.g., ministral-3:3b), the system prompt uses an abbreviated XML tool listing instead of full OpenAI-style function definitions. Saves context tokens; see [`tools/registry.py: format_for_llm(compact=True)`](dragon_voice/tools/registry.py).

**`config_update`** — WS message Tab5 sends to swap `voice_mode` and/or `llm_model`. Dragon hot-swaps the relevant backends (or, if router-active, just calls `set_voice_mode`). Per-connection deep-copy of config means two Tab5s on the same Dragon can be in different modes simultaneously. Confirmed back to Tab5 via the same message type with the applied config.

**ConversationEngine** — [`dragon_voice/conversation.py`](dragon_voice/conversation.py). The orchestrator for a multi-turn LLM conversation with memory + tools + history. Entry point: `process_text_stream(session_id, text, media_id=None, ...)`. Voice turns, text turns, and (post-#186) vision turns all converge here.

**Cross-modal continuity** — Property guaranteed by PR #186: a vision turn persists the user's photo into `messages` table via the multimodal-marker encoding; subsequent text turns load it back hydrated to OpenAI `image_url` content arrays via `MessageStore.get_context(media_store=…)`. Means "send a photo, then ask follow-up text" works.

## D

**Device** — A registered hardware client (Tab5 instances, browser at `/call`, etc.). Persists in `devices` table. Identified by stable `device_id` (Tab5 uses MAC). Survives reboots; sessions belong to devices.

**Dictation mode** — Long-form voice recording. Tab5 sends `{"type":"start","mode":"dictate"}`. Dragon emits `stt_partial` events as you speak. Auto-stops after 5 s of silence (`DICTATION_AUTO_STOP_FRAMES=250`). Post-processing runs the transcript through the LLM to generate a title + summary, returned via `dictation_summary`.

**Dragon** — The brain of TinkerClaw. Snapdragon QCS6490 ARM SBC running Ubuntu + the `dragon_voice` Python service on port 3502. Hosts STT, LLM (router-fronted), TTS, embeddings, conversation engine, tools, memory, dashboard, OTA. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

**`DRAGON_API_TOKEN`** — Bearer token for Dragon's `/api/*` REST surface. Lives in `/home/radxa/.env` (loaded by systemd `EnvironmentFile=`). Different from Tab5's `auth_tok` (which gates the Tab5 firmware debug server on port 8080).

**Dual** — Old two-backend orchestration (`backend: "dual"`). Picker model emits a tool call; responder model writes the user-visible reply. Predates the multi-model router. Still supported but the router (`backend: "router"`) is more general.

## E

**Embedding model** — `nomic-embed-text` running locally via Ollama. 768-dimensional vectors. Used for memory facts + document chunks + RAG search. See `MemoryService`.

**Empty-reply guard** — [`tools/response_wrap.py: synthesize_wrap`](dragon_voice/tools/response_wrap.py). When an FC-trained model emits a tool call but no user-visible text, this synthesizes a one-line natural-language ack from the tool result so the user doesn't see silence. Triggered by `looks_like_useful_text(response) == False` AND at least one tool fired.

**`/events` ring** — Tab5 firmware-side observability. Ring buffer of 256 events with `kind` + `detail` + `ms` (uptime). Populated by `tab5_debug_obs_event()` calls at key sites; queried via `GET /events?since=<ms>`. Used by the e2e harness to know when state changed.

## F

**Fleet** — `LLMConfig.fleet: list[ModelSpec]` in [`config.py`](dragon_voice/config.py). The list of LLM backends the router can pick from. Each spec declares `id`, `backend`, `model_id`, `caps`, `tier`, `priority`, `keep_alive_s`. Empty list + non-`router` backend = legacy single-backend dispatch.

**`fleet_summary`** — Dragon → Tab5 protocol field on `session_start.config` and `config_update` ACK. `dict[Modality, model_id | null]` — the model the router would currently pick per modality at the active tier. Lets Tab5 firmware light up dynamic capability chips. Only present when `llm == "router"`.

**FreeRTOS** — Tab5's RTOS. Tasks, mutexes, semaphores, queues. Most Tab5 firmware concurrency is FreeRTOS-task-based (one task per long-running concern: WS RX, mic capture, video stream, etc.). Stack allocations are tight on the dual-core RISC-V; PSRAM-stack tasks via `xTaskCreatePinnedToCoreWithCaps`.

**Full Cloud** → see **Cloud mode**.

## G

**Genie** → see **NPU Genie**.

**Glyph OS** — Internal codename for the Tab5 visual design language. Currently v4·C ("Ambient Canvas"). v3 designs (`stitch-designs/v3/`) are local-only design exploration; never committed to the repo.

## H

**Hybrid mode** — `voice_mode = 1`. STT and TTS go cloud (OpenRouter `gpt-audio-mini`); LLM stays local (ollama). Best dollar-per-turn for a snappy assistant. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## I

**`infer_required_caps`** — [`router.py`](dragon_voice/llm/router.py) function. Walks an OpenAI-format `messages` list, infers what `Modality` set the request needs: image_url → +VISION, video_url → +VIDEO, input_audio → +AUDIO_IN. Always includes TEXT. **Does NOT** infer TOOL_CALLING — see PR #186 for why (would gate vision turns from picking MiniCPM-V which lacks tools).

**`input_mode`** — `messages.input_mode` column. CHECK constraint allows `'voice' | 'text' | 'system'`. Vision turns use `'text'` because the multimodal-ness lives in the content marker, not the input_mode. A migration to add `'vision'` is documented but not shipped.

## L

**LAN tier** — Router tier for backends running on the same LAN as Dragon, like a workstation hosting LM Studio. Eligible in voice_mode 2 (Cloud) alongside the cloud tier. Lets you run heavy multimodal models off-Dragon at zero per-token cost.

**`LLMBackend`** — Abstract base in [`llm/base.py`](dragon_voice/llm/base.py). Six concrete subclasses (ollama, openrouter, lmstudio, npu_genie, tinkerclaw, dual) plus the router (which IS-A LLMBackend, holding N sub-backends). All implement `generate_stream(prompt)` (legacy) and `generate_stream_with_messages(messages)` (multimodal-aware).

**`llm_done`** — WS message Dragon sends after the final LLM token of a turn streams. Carries `llm_ms` (latency). Tab5's debug-obs system also fires a `chat.llm_done` event here for the e2e harness.

**Local mode** — `voice_mode = 0`. STT (Moonshine), LLM (ollama or NPU), TTS (Piper) all on Dragon. Privacy-first; slow on Q6A CPU (~60-90 s/turn for ministral-3:3b). The default.

**Long-press** — Tab5 touch event. The `/touch` debug endpoint accepts `action:"long_press"` with `duration_ms` 500-5000. LVGL fires `LV_EVENT_LONG_PRESSED` at 400 ms by default.

## M

**MCP** — Model Context Protocol. Anthropic's tool/resource discovery protocol. Dragon ships an MCP client + bridge in [`dragon_voice/mcp/`](dragon_voice/mcp/) that registers external MCP servers as Dragon tools.

**MediaStore** — [`dragon_voice/media/store.py`](dragon_voice/media/store.py). Disk-backed (`/home/radxa/media/`) blob store for rich media. 24h TTL with hourly cleanup. 500 MB cap. Files named `<sha256>.jpg`. Both Dragon-rendered media (Pygments code, Pillow tables) and Tab5-uploaded camera photos live here.

**MediaPipeline** — [`dragon_voice/media/pipeline.py`](dragon_voice/media/pipeline.py). After an LLM turn finishes, scans the response for code blocks / tables / image URLs and renders them as JPEG via Pygments + Pillow. Up to 3 media items per response. Strips the rendered content from the text via `text_update`.

**Memory facts** — Persisted user-or-LLM-stated facts in `memory_facts` table. Embedded with `nomic-embed-text` (768-dim). Auto-recalled before every LLM call via cosine similarity against the user's query. Stored via `remember` tool, retrieved via `recall` tool, browsed via dashboard's Memory tab.

**`messages` table** — Append-only log of every conversation message. `(id, session_id, role, content, input_mode, created_at, model, latency_ms, ...)`. Multimodal user messages encode the photo reference into `content` via the `__mm__:` marker prefix.

**ministral-3:3b** — Default Local-mode LLM model. 2.8 GB. ~65 s median per turn on Dragon Q6A. Selected from the 11-model gauntlet documented in [`docs/historical/AUDIT-WAVE-14.md`](docs/historical/AUDIT-WAVE-14.md). Best correct-tool-fire rate (7/10) of all sub-4B models tested; only sub-4B model that fits Tab5's WS keepalive ceiling and does non-trivial math correctly.

**Modality** → see **Capability**.

**Moonshine** — STT model running on Dragon CPU. `medium-streaming-en` quantized variant via `moonshine_voice` package. ~400-800 ms for 2-second utterances on Q6A.

**Multimodal marker** — `__mm__:` prefix on `messages.content`. Indicates the row is a JSON-encoded `{media_id, text}` pair, not plain text. `_hydrate_multimodal_content` in [`messages.py`](dragon_voice/messages.py) decodes it back into the OpenAI multimodal content-array format at context-build time.

## N

**ngrok** — Tunnels Dragon's local services to public domains: `tinkerclaw-voice.ngrok.dev` (port 3502), `tinkerclaw-dashboard.ngrok.dev` (3500), `tinkerclaw-gateway.ngrok.dev` (18789). Single systemd unit `tinkerclaw-ngrok.service` maintains all three. Tab5 firmware tries LAN first then ngrok fallback, so the device works off-network.

**`nomic-embed-text`** — 768-dim embedding model running via Ollama. Used everywhere — memory facts, document chunks, search queries.

**NPU Genie** — Qualcomm QAIRT-based LLM inference path on Dragon's Hexagon V68 NPU. Currently runs Llama 3.2 1B at ~8 tok/s. Text-only (no vision support). Setup in [`docs/npu-setup.md`](docs/npu-setup.md). Backend ID `"npu_genie"`.

**NVS** — Tab5's non-volatile storage. ESP-IDF flash partition holding key-value settings. See TinkerTab `CLAUDE.md` "NVS Settings Keys" for the full table.

## O

**OPUS** — Audio codec for the WS streaming path. Capability negotiation works end-to-end (PR #174 / TinkerTab #263/#265). Decoder ready on Tab5; **encoder gated OFF** in `voice_codec.h` pending TinkerTab #264 — SILK NSQ crashes mid-frame on ESP32-P4. Don't enable uplink until #264 closes.

## P

**`peek=True`** — Parameter on `Tab5Driver.events()` in the e2e harness. When True, returns events without advancing the global cursor. Used by diagnostic snapshots so they don't steal events that subsequent `await_event` calls need. See LEARNINGS #92.

**Piper** — TTS engine on Dragon CPU. `en_US-lessac-medium` voice. Outputs 22 050 Hz mono int16 PCM, resampled to 16 kHz before sending to Tab5. ~real-time (1× factor).

**Priority** (router) — `ModelSpec.priority` int. Lower wins. Use gaps (0, 5, 10, 15) so new models can slot in without renumbering. Across same-tier same-cap candidates, the lowest-priority spec is chosen.

**Pause / resume** — Session lifecycle states. WS connection drops → session goes `paused`. Reconnect → session resumes (server replays last 20 messages so chat overlay rehydrates). Pause-aged-out sessions (default 30 days, `paused_session_retention_days`) become `ended`.

## R

**RAG** — Retrieval-augmented generation. Memory facts + document chunks are retrieved via cosine similarity against the user's query, then injected into the system prompt before the LLM call. See `MemoryService.get_relevant_context`.

**Relay** — Dragon's role in a video call. **Dumb fan-out broadcast**: receives `VID0` or `AUD0` binary frames from one connection and forwards them verbatim to all others. No transcoding, no buffering beyond the WS write queue. See [`flows/video-call.md`](docs/flows/video-call.md).

**Router** → see **`CapabilityAwareRouter`**.

## S

**Session** — A multi-turn conversation. Belongs to a device. `id, device_id, type:"conversation"|"recording", system_prompt, status:"active|paused|ended", created_at, last_active_at, message_count`. Survives WS reconnect (within the pause window).

**`session_start`** — First substantive Dragon → Tab5 message after `register`. Carries `session_id`, `resumed`, `message_count`, and a `config` blob (incl. `fleet_summary` when router is active). Tab5 transitions from CONNECTING to READY here.

**`set_voice_mode`** — Method on `CapabilityAwareRouter`. Flips the tier policy without rebuilding the fleet. Instantiated sub-backends survive — saves the cost of re-loading models on every voice-mode change.

**Skill** — User-facing pluggable feature on Dragon. Currently scaffolded — see [`docs/SKILL_AUTHORING.md`](docs/SKILL_AUTHORING.md) and TinkerTab's [`docs/WIDGETS.md`](https://github.com/lorcan35/TinkerTab/blob/main/docs/WIDGETS.md). Skills emit *widgets* (live, card, list, chart, media, prompt) into Tab5 surface slots. Time Sense (Pomodoro) is the reference skill.

**`spent_mils`** / **`spent_day`** — Tab5 NVS keys tracking today's cumulative LLM spend in mils. Resets when `spent_day` rolls past local midnight. Compared against `cap_mils` to trigger auto-downgrade.

**Surface** — Tab5 widget abstraction. Six surface types: live (one at a time, full home-card), card (transient), list (5 items max), chart (12 points max), media, prompt (3 choices max). Skills emit widget state via WS; Tab5 renders opinionatedly using v4·C theme tokens.

## T

**Tab5** — The face of TinkerClaw. M5Stack Tab5 (ESP32-P4) running native LVGL UI. Thin client — owns LVGL UI + mic/speaker/camera/touch + WiFi + NVS, sends/receives over WS to Dragon. See [TinkerTab](https://github.com/lorcan35/TinkerTab) repo.

**`tab5_lv_async_call`** — `void tab5_lv_async_call(lv_async_cb_t cb, void *arg)` in TinkerTab's [`main/ui_core.{c,h}`](https://github.com/lorcan35/TinkerTab/blob/main/main/ui_core.c). Wrapper around LVGL's `lv_async_call` that takes the LVGL recursive mutex first. **Always use this**, never the LVGL primitive directly — `lv_async_call` is NOT thread-safe (does `lv_malloc` + `lv_timer_create` against unprotected TLSF; #257/#259 closed the long-residual stability class with this).

**`task_worker`** — Shared FreeRTOS job queue on Tab5 ([`task_worker.{c,h}`](https://github.com/lorcan35/TinkerTab/blob/main/main/task_worker.c)). 16 KB stack on PSRAM. Long-running uploads / downloads / etc. enqueue here instead of spawning per-action tasks (which leaked under load — see PR #285 for the persistent-task pattern).

**Tier** — Router term for the deployment cohort of a model. One of `local`, `cloud`, `lan`. `TIER_FOR_MODE` maps voice_mode to the set of tiers the router will pick from. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

**TinkerClaw Gateway** — Optional sidecar agent runner on Dragon, port 18789 localhost. Activated by `voice_mode = 3`. Owns its own LLM choice + tool execution + browser automation. Dragon becomes an audio pipe in this mode (STT + TTS still local, but ConversationEngine + ToolRegistry + MemoryService are bypassed).

**Tool-calling dialect** — Three accepted XML formats:
1. *Legacy:* `<tool>NAME</tool><args>{json}</args>` (TinkerBox system-prompt format)
2. *Standard:* `<tool_call>{"name":"...","arguments":{...}}</tool_call>` (industry-typical FC fine-tunes)
3. *Bracketed-name:* `[NAME]{json}</NAME>` (xLAM quirk — gated on registered tool names)

See [`tools/registry.py`](dragon_voice/tools/registry.py) module docstring.

**TOOL_CALLING** (Modality) — Declared by backends trained for function-calling. Informational, NOT a router gate — would otherwise force every turn through a tool-capable model even when none is needed. Tool detection happens at runtime via the parser.

## V

**`VID0`** — 4-byte ASCII magic on a binary WS frame indicating it's a JPEG video frame for the call relay. See [`flows/video-call.md`](docs/flows/video-call.md).

**Vision capability** event — Dragon → Tab5 `vision_capability` WS message. Tells Tab5 whether the active LLM (or fleet's tier-aware vision pick) can do vision, what the model id is, and the per-frame cost in mils. Drives the camera-screen capability chip. Pre-#186 this was substring-based; post-#186 it's capability-driven.

**Voice mode** → see **Local mode**, **Hybrid mode**, **Cloud mode**, **TinkerClaw Gateway**.

**`VOICE_MODE_CALL`** — Tab5-internal voice-pipeline state. Independent of `voice_mode` 0/1/2/3. When active: mic frames are wrapped with `AUD0` and broadcast to other call participants instead of being fed to STT. See [`flows/video-call.md`](docs/flows/video-call.md).

## W

**Wave audit** — Periodic systematic audit of the cross-stack codebase. Wave 13 closed in 2026-04-21 (16 C/H items merged). Wave 14 closed (see [`docs/historical/AUDIT-WAVE-14.md`](docs/historical/AUDIT-WAVE-14.md)). Wave 15 active ([`docs/AUDIT-WAVE-15.md`](docs/AUDIT-WAVE-15.md)).

**Widget** — Skill-emitted state rendered by Tab5. Six types — live, card, list, chart, media, prompt. Skills never write layout; the widget vocabulary IS the layout contract. See TinkerTab's `docs/WIDGETS.md`.

**WS** — WebSocket. Specifically `/ws/voice` on Dragon port 3502 — the single channel Tab5 uses for register / voice / chat / vision / video / tool events / config updates. See [`protocol.md`](docs/protocol.md).

## Cross-references

- **Operating runbook** (deploy, debug, restart, monitor): [`CLAUDE.md`](CLAUDE.md)
- **System architecture**: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- **Wire format**: [`docs/protocol.md`](docs/protocol.md)
- **Per-flow traces**: [`docs/flows/`](docs/flows/)
- **Router fleet recipes**: [`docs/router-cookbook.md`](docs/router-cookbook.md)
- **Skills + widgets**: [`docs/SKILL_AUTHORING.md`](docs/SKILL_AUTHORING.md)
- **NPU setup**: [`docs/npu-setup.md`](docs/npu-setup.md)
- **Lessons + gotchas**: [`LEARNINGS.md`](LEARNINGS.md)
- **Closed audits**: [`docs/historical/`](docs/historical/)
