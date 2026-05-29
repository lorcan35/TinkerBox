---
audience: developer
type: reference
prerequisites: none
last-verified: 2026-05-29
---
# dragon_voice package map

This is the lookup table for the `dragon_voice/` Python package — the
[Dragon](../../GLOSSARY.md) voice server that runs on port 3502 and holds all of
TinkerBox's intelligence. Use it to answer "which file owns this concern?"
before you add code or chase a bug. Each row names a module and the single
responsibility it owns.

The package was decomposed from a 2,747-LOC monolithic `server.py` under umbrella
issue [#65](https://github.com/lorcan35/TinkerBox/issues/65) (closed 2026-04-24)
into four sibling sub-packages (`middleware/`, `handlers/`, `lifecycle/`, plus the
slimmed `server.py`), then grew a wide layer of single-concern modules directly
under the package root as later UX-gap fixes, the [multi-model router](../router-cookbook.md),
and the W7 channel/agent work landed. Every extracted module takes its
dependencies explicitly (a server handle or specific args), so each can be
imported and unit-tested without instantiating `VoiceServer`.

## How to read this map

- **Top-level modules** (`dragon_voice/*.py`) split into two groups: a handful of
  *core* modules (server, pipeline, conversation, sessions, messages, db, memory,
  config) and ~60 *single-concern* sibling modules whose name describes the one
  thing they own (`config_swap.py`, `vision_turn.py`, `channel_reply_handler.py`, …).
- **Sub-packages** (`dragon_voice/<name>/`) group a family: `stt/`, `tts/`, `llm/`,
  `api/`, `tools/`, and so on.
- Anything not under `dragon_voice/` (the repo-root files, `tests/`, `docs/`) is in
  the [Repo-root companions](#repo-root-companions) table at the end.

To re-derive the live counts on a checkout:

```bash
ls dragon_voice/*.py | wc -l          # top-level modules (73 as of 2026-05-29)
ls dragon_voice/                      # sub-packages + modules
grep -c 'app.router.add_' dragon_voice/api/*.py dragon_voice/notes/api.py \
  | awk -F: '{s+=$2} END {print s}'   # REST endpoint count
```

## Core modules

The eight modules that define the server's spine. These are the ones you read
first to understand a request's path through Dragon.

| File | Owns | Key types |
|---|---|---|
| `dragon_voice/__main__.py` | Entry point for `python3 -m dragon_voice` | — |
| `dragon_voice/__init__.py` | Package init | — |
| `dragon_voice/server.py` | `VoiceServer` class + `create_app` wiring + the WS-voice handler family (register / text / user_media / disconnect / audio + event hooks). ~2,720 LOC | `VoiceServer` |
| `dragon_voice/pipeline.py` | STT → LLM → TTS orchestration with VAD, dictation, and post-processing. Mode-aware timeouts live here (Local 300 s, Cloud 60 s, TinkerClaw 180 s) | `VoicePipeline` |
| `dragon_voice/conversation.py` | Multi-turn `ConversationEngine`: tool-calling loop + memory-augmented context build | `ConversationEngine` |
| `dragon_voice/sessions.py` | `SessionManager` — create / resume / pause / end lifecycle; sessions survive disconnect | `SessionManager` |
| `dragon_voice/messages.py` | `MessageStore` — append-only message log, LLM context builder, multimodal `__mm__:` content marker encode/decode | `MessageStore` |
| `dragon_voice/db.py` | Async SQLite layer (aiosqlite, WAL mode, full CRUD) | `Database` |
| `dragon_voice/memory.py` | `MemoryService` — facts + documents + RAG via `nomic-embed-text` (768-dim) Ollama embeddings | `MemoryService` |
| `dragon_voice/config.py` | Config dataclasses (`STTConfig`, `TTSConfig`, `LLMConfig`, `ToolsConfig`, `MemoryConfig`, fleet) + YAML/env loading | dataclasses |
| `dragon_voice/config.yaml` | Default configuration (see the [config reference](config-reference.md)) | — |
| `dragon_voice/requirements.txt` | Voice-pipeline Python dependencies | — |
| `dragon_voice/errors.py` | Shared error types + WS error-code constants | — |
| `dragon_voice/progress.py` | β-arch unified progress event model (issue #123) | — |

The DB is further sliced into per-table helper modules so a single `db.py` no
longer holds every query: `db_config.py`, `db_devices.py`, `db_events.py`,
`db_messages.py`, `db_notes.py`, `db_sessions.py`, plus `db_corruption_recovery.py`
(boot-time integrity check + rebuild) and `device_upsert.py`.

## WS-voice handler family (top-level siblings)

These modules each own one branch of the WebSocket voice handler or one stage of a
turn. `server.py` dispatches into them; they take an explicit server/connection
handle so they unit-test in isolation. Find the full live list with
`ls dragon_voice/*.py`.

| File | Owns |
|---|---|
| `ws_voice_admission.py` | Admission control for a new `/ws/voice` connection (the P13 "device already has connection" guard) |
| `session_handshake.py` | `register` frame → device upsert → session create/resume → `session_start` reply |
| `conn_state.py` | Per-connection state object shared across the handler family |
| `start_handler.py` | `start` frame — begins a voice turn, clears the audio buffer |
| `stop_handler.py` | `stop` frame — finalizes the turn (ask mode → STT→LLM→TTS; dictate mode → transcript) |
| `cancel_handler.py` | `cancel` frame — aborts in-flight LLM/TTS generation |
| `clear_handler.py` | `clear` frame — ends the session and starts a fresh one |
| `disconnect_handler.py` | WS close → session → `paused`, cleanup |
| `stale_conn_eviction.py` | Evicts a stale prior connection when a device reconnects |
| `binary_frame_dispatch.py` | Routes binary frames: untagged → mic PCM for STT; `AUD0`/`VID0` → call relay |
| `local_text_stream.py` | Local-mode text turn through `ConversationEngine` |
| `tinkerclaw_text_path.py` | voice_mode=3 text bypass straight to the TinkerClaw gateway adapter |
| `text_turn_gate.py` / `text_path_receipt.py` / `text_path_tts.py` | Text-turn gating, receipt emission, and optional TTS of a text reply |
| `voice_path_receipt.py` | Receipt emission for the voice path |
| `vision_turn.py` | Multimodal (image) turn — routes to a vision-capable backend |
| `vision_capability.py` | Emits the `vision_capability` event (camera-screen capability chip) |
| `cap_downgrade.py` | Capability downgrade when the active backend cannot serve a requested modality |
| `token_flush.py` | Sentence-boundary token buffering for low-latency TTS |
| `speak_system.py` | Speaks a system-generated utterance (e.g. notifications) |
| `dictation_audio.py` / `dictation_classifier.py` / `dictation_post.py` | Dictation segment buffering, intent classification, and post-process (LLM title + summary → `dictation_summary`) |
| `empty_response_wrap.py` | Synthesizes a one-line ack when a model fires a tool then emits no visible text (see `tools/response_wrap.py`, #77/#79) |
| `tool_event_emitter.py` | Emits `tool_call` / `tool_result` WS events during agentic turns |
| `widget_action_handler.py` / `widget_capabilities_init.py` | Tab5 widget-surface action handling + capability init |
| `channel_reply_handler.py` | `channel_reply` frame → forward to `ChannelConnector` → emit `channel_reply_ack` (W7-F) |
| `gateway_memory_mirror.py` | Mirrors memory facts to the gateway for voice_mode=3 continuity |
| `codec_negotiation.py` / `audio_codec.py` | Codec capability negotiation (OPUS gated off pending TinkerTab #264) |
| `progress_emit.py` / `rich_media_emit.py` | Progress-event and rich-media (`media`/`card`/`text_update`) emission |
| `video_upstream.py` | `VID0`/`AUD0` fan-out relay for two-way calls — verbatim broadcast, no transcode |
| `ws_keepalive.py` | Fires `ws.ping()` every 5 s during inference so slow local turns don't trigger a client reconnect (#76) |
| `handler_task_spawn.py` / `pipeline_callbacks.py` / `pipeline_init.py` | Async task spawning, pipeline callback wiring, per-connection pipeline init (resets to Local defaults on reconnect) |
| `config_swap.py` / `config_swap_guards.py` / `pool_aware_swap.py` / `backend_swap.py` | Hot backend swap on `config_update`, with validation guards + backend-pool awareness |
| `config_update_ack.py` / `config_update_rate_limit.py` / `config_finalize.py` | `config_update` ACK, server-side coalescing/rate-limit, and finalization |
| `fallback_stt_cache.py` / `fallback_tts_cache.py` | Cached local STT/TTS instances for cloud auto-fallback |
| `async_agent.py` | Async agent-runner glue |
| `coredump_scraper.py` | Scrapes Tab5 coredumps surfaced over the protocol |

> The exact set of siblings moves as concerns are extracted. When in doubt, the
> live tree is authoritative — run `ls dragon_voice/*.py`. The map above is
> verified against the tree on the `last-verified` date.

## `api/` — REST API package

Modular REST route classes wired by `setup_all_routes()`. Endpoint reference:
[REST API](rest-api.md). Re-count endpoints with the `grep` above.

| File | Routes |
|---|---|
| `__init__.py` | `setup_all_routes()` — wires every route class |
| `utils.py` | Shared helpers (`json_error`, pagination) |
| `sessions.py` | Session CRUD + lifecycle (`/api/v1/sessions*`) |
| `messages.py` | Message listing + SSE chat (`/api/v1/sessions/{id}/{messages,chat}`) |
| `devices.py` | Device CRUD (`/api/v1/devices*`) |
| `config_routes.py` | Config CRUD + delete (`/api/v1/config*`) |
| `events.py` | Event listing with device filter (`/api/v1/events`) |
| `agent_log.py` | Cross-session tool-call ring buffer (`/api/v1/agent_log`) — `source` field buckets dragon/gateway/channel_push/user_reply (W7-A.3) |
| `agent_skills.py` | W7-B merged catalog of OpenClaw core tools + observed tool names (`/api/v1/agent_skills`) |
| `synthesize.py` | TTS synthesis + STT transcription + OTA routes |
| `completions.py` | Stateless direct-LLM completion (`/api/v1/completions`) |
| `system.py` | System metrics + backend listing |
| `tools.py` | Tool listing + direct execution (`/api/v1/tools*`) |
| `memory_routes.py` | Memory-fact CRUD + semantic search |
| `documents.py` | Document ingest + listing + chunk search |
| `media_routes.py` | `GET /api/media/{id}` (serve), `POST /api/media/upload` (Tab5 camera) |
| `scheduler.py` | 5 endpoints for the notification scheduler (`/api/v1/scheduler/notifications*`) |
| `spend.py` | W5-A daily LLM spend roll-up (`/api/v1/spend`) — backed by `billing/spend_tracker.py` |
| `integrations.py` | Calendar / Gmail / Tasks connect/disconnect/list/test (`/api/v1/integrations/*`) |
| `video_inject.py` | Debug-only `POST /api/video/inject` — push a JPEG as if from a paired Tab5 (#178) |
| `debug_channel.py` | W7-F stub `POST /api/v1/debug/channel_message` — fan a synthetic frame to a Tab5 |
| `logs_tail.py` | Tail server logs over REST for diagnostics |
| `coredumps.py` | Coredump listing/retrieval endpoints |

## `handlers/` — extracted HTTP endpoint handlers

Stateless HTTP handlers pulled out of `VoiceServer` in #65. Each takes explicit deps.

| File | Owns |
|---|---|
| `__init__.py` | Re-exports submodules |
| `status.py` | `handle_status` (HTML status page) + `handle_health` (JSON liveness) |
| `config_api.py` | `handle_get_config` (redacted dump) + `handle_set_config` (hot-reload + lock-guarded backend swap) |
| `debug.py` | `handle_debug_mem` (tracemalloc/RSS/gc probe) + `handle_debug_widget_{chart,prompt,card,media}` audit emitters |

## `lifecycle/` — boot, shutdown, monitors

| File | Owns |
|---|---|
| `__init__.py` | Re-exports submodules |
| `startup.py` | `run_startup(server, app)`: DB → sessions → memory + tools → surfaces → conversation → REST routes → notes → MCP → periodic tasks |
| `shutdown.py` | `run_shutdown(server, app)`: cancel+await monitors → drain pipelines → release backend pool → close HTTP clients |
| `monitors.py` | `get_rss_mb`, `get_cpu_temp`, `memory_monitor_loop` (5-min RSS/temp/FD sample + pipeline drain on critical) |
| `purge.py` | `periodic_purge_loop` (message retention) + `media_cleanup_loop` (24 h media auto-cleanup) |
| `agentic_init.py` | Tool registry + memory + document service init |
| `notes_init.py` | Notes service init (wires `NoteTool` after the service exists) |
| `integrations_init.py` | Calendar / Gmail / Tasks integration init |
| `mcp_init.py` | MCP client/bridge init |
| `surfaces_scheduler_init.py` | SurfaceManager + scheduler init (`timesense_tool` registers after SurfaceManager) |
| `background_tasks_init.py` | Spawns the periodic background tasks |

## `middleware/` — aiohttp request middleware

Each is stateless with explicit deps. See the [config reference](config-reference.md)
for the security-related knobs.

| File | Owns |
|---|---|
| `__init__.py` | Re-exports submodules |
| `cors.py` | `handle_cors` + `DEFAULT_ALLOWED_ORIGINS` (SEC12) |
| `security_headers.py` | `handle_security_headers` + `SECURITY_HEADERS` (W14-M06) |
| `auth.py` | `handle_auth` + `PUBLIC_PREFIXES` (Wave 13 bearer-token gate) |
| `rate_limit.py` | `handle_rate_limit` + `DEFAULT_RATE_LIMIT_RULES` (W15-H01/H06) |

## `llm/` — LLM backends + multi-model router

The router is opt-in (`backend: "router"` + a populated `fleet`). Full recipes:
[router cookbook](../router-cookbook.md). To swap the active backend, see
[Swap the LLM backend](../how-to/swap-the-llm-backend.md); to build a fleet, see
[Configure the multi-model router](../how-to/configure-the-multi-model-router.md).

| File | Owns |
|---|---|
| `base.py` | `LLMBackend` ABC + `Modality` enum + `capabilities` default |
| `router.py` | `CapabilityAwareRouter`, `ModelSpec`, `TIER_FOR_MODE`, `infer_required_caps`, `summarize` |
| `capability_registry.py` | Per-backend modality declarations |
| `ollama_llm.py` | Ollama backend (OpenAI → Ollama multimodal content translator added in #186) |
| `openrouter_llm.py` | OpenRouter backend + `_OPENROUTER_CAPS` + `_PRICING_MILS_PER_M` (35 models, sync 2026-04-27) |
| `lmstudio_llm.py` | LM Studio / llama-server OpenAI-compatible backend (LAN tier; the live Local default path) |
| `npu_genie.py` | Qualcomm QAIRT/Genie backend — text-only, ~8 tok/s on the QCS6490 HTP |
| `tinkerclaw_llm.py` | TinkerClaw gateway adapter (voice_mode=3) |
| `dual.py` | Two-backend picker+responder (predates the router) |

## `stt/` and `tts/` — speech backends

| `stt/` file | Backend key | | `tts/` file | Backend key |
|---|---|---|---|---|
| `base.py` | `STTBackend` ABC | | `base.py` | `TTSBackend` ABC |
| `moonshine_stt.py` | `moonshine` | | `piper_tts.py` | `piper` (22050 Hz) |
| `whisper_cpp.py` | `whisper_cpp` | | `kokoro_tts.py` | `kokoro` |
| `vosk_stt.py` | `vosk` | | `edge_tts_backend.py` | `edge_tts` |
| `openrouter_stt.py` | `openrouter` | | `openrouter_tts.py` | `openrouter` |
| | | | `registry.py` | TTS backend factory |
| | | | `text_cleaner.py` | Pre-synthesis text normalization |

`tts/` also carries experimental backends `kitten_tts.py`, `neutts_air_tts.py`, and
`supertonic_tts.py`. The mode-to-backend mapping is in the
[voice-modes reference](voice-modes.md).

## `tools/` — tool-calling infrastructure

The tool parser accepts multiple dialects; the canonical catalog of tools and their
schemas is in the [tools catalog](tools-catalog.md), and the authoring workflow is
[Add a tool](../how-to/add-a-tool.md).

| File | Owns |
|---|---|
| `__init__.py` | Exports `ToolRegistry`, `Tool` |
| `base.py` | `Tool` abstract base class |
| `registry.py` | `ToolRegistry`: register, parse XML markers, execute. `execute()` feeds the `agent_log` ring buffer (single canonical instrumentation site) |
| `parser.py` | Tool-marker parser — 5 accepted dialects (legacy / standard / bracketed-name / Gemma `<\|tool_call>` / LFM `<\|tool_call_start\|>`) |
| `formatter.py` | Renders tool defs into the compact system-prompt format for local models |
| `response_wrap.py` | `synthesize_wrap` — per-tool natural-language ack templates (empty-reply guard, #77/#79) |
| `web_search.py` | SearXNG-backed search (DuckDuckGo fallback), port 8888 |
| `memory_tools.py` | `StoreFactTool` + `RecallFactsTool` + `ForgetFactTool` (confirm-gated) |
| `datetime_tool.py`, `calculator_tool.py`, `unit_converter_tool.py`, `weather_tool.py`, `system_tool.py`, `stock_ticker_tool.py`, `timer_tool.py` | Individual built-in tools |
| `timesense_tool.py` | Pomodoro + `widget_live` emitter (registered after SurfaceManager) |
| `quick_poll_tool.py` | Declarative widget-skill reference (Wave 12) |
| `note_tool.py` | `NoteTool` (registered after `NotesService`) |
| `google_calendar_tool.py`, `gmail_tool.py`, `schedule_reminder_tool.py` | Integration-backed tools (Calendar / Gmail / scheduler) |

## Other sub-packages

| Package | Owns | Key files |
|---|---|---|
| `notes/` | Notes CRUD + search + audio ingestion | `db.py`, `service.py`, `api.py` (`/api/notes/*`) |
| `media/` | Rich-media rendering | `store.py` (MediaStore, 24 h cleanup, 500 MB cap), `pipeline.py` (Pygments/Pillow render), `url_signer.py` (HMAC-signed `/api/media/{id}`) |
| `surfaces/` | Tab5 widget abstraction (`live`/`card`/`list`/`chart`/`media`/`prompt`) | `base.py`, `manager.py` (`SurfaceManager`) |
| `scheduler/` | In-process notification scheduler + sqlite-backed store | `manager.py`, `models.py`, `parser.py` (`parse_when`), `store.py` (in-memory + sqlite replay/queue/snooze) |
| `channels/` | W7-F third-party messaging connectors | `base.py` (`ChannelConnector`), `gateway.py` (`GatewayConnector`, ed25519 signed-connect, `_TAB5_CHANNEL_ALIASES`), `device_identity.py` (`~/.dragon/identity/device.json`), `mock.py` |
| `billing/` | LLM spend tracking | `spend_tracker.py` (backs `/api/v1/spend`) |
| `mcp/` | Model Context Protocol client + bridge | `client.py`, `bridge.py` |

## Repo-root companions

Files outside `dragon_voice/` that the server depends on or ships beside.

| Path | Owns |
|---|---|
| `schema.sql` | Database schema — 11 tables (6 foundation + 3 memory + 2 scheduler) |
| `dashboard.py` | Web dashboard (port 3500, 11-tab SPA, proxies `/api/proxy/*` to the voice server) |
| `tests/` | Test suite — CI named-set runs without a live server; `test_api_e2e.py` (29 live scenarios) + `test_e2e_dragon.py` are local-only |
| `docs/` | Audience-facing Diátaxis docs (see [the docs index](../README.md)) |
| `systemd/` | Canonical `tinkerclaw-*.service` unit files |
| `CLAUDE.md` | The runbook — deploy / debug / restart / monitor, and the source-of-truth File Structure section |
| `LEARNINGS.md` | Institutional knowledge (bugs / fixes / gotchas) |

> **Legacy files:** `dragon_server.py` (CDP browser streaming, port 3501) and
> `udp_streamer.py` (UDP JPEG streaming) were retired Tab5-side in #155
> ("voice-first is the product"). If copies linger on a deployed Dragon they are
> no longer wired up by systemd.

## Where to go next

| Goal | Read |
|---|---|
| Look up a REST endpoint | [REST API reference](rest-api.md) |
| Look up a WebSocket frame | [WebSocket protocol reference](websocket-protocol.md) · [`docs/protocol.md`](../protocol.md) |
| Look up a config key | [Config reference](config-reference.md) |
| Look up a tool | [Tools catalog](tools-catalog.md) |
| See the system map | [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md) |
