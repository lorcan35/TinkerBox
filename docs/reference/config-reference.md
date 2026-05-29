---
audience: integrator
type: reference
prerequisites: [router cookbook](../router-cookbook.md), [REST API reference](rest-api.md)
last-verified: 2026-05-29
---
# config.yaml reference

Every Dragon-side setting lives in one file: `dragon_voice/config.yaml`. It is
loaded at startup by `dragon_voice/config.py` into a tree of dataclasses, so the
authoritative list of fields, types, and defaults is the dataclass definitions —
this page mirrors them field-by-field.

`config.yaml` is the lowest-priority layer. Three layers stack on top of it:

| Layer | Wins over | How |
|---|---|---|
| `config.yaml` | — | YAML file next to `config.py`, or the path in `DRAGON_VOICE_CONFIG` |
| Environment variables | the file | `DRAGON_VOICE_{SECTION}_{KEY}` (type-coerced), plus the special vars below |
| CLI flags | env + file | `--port`, `--host`, `--stt`, `--tts`, `--llm`, `--log-level`, `--config` |
| REST hot-reload | runtime config | `POST /api/config` swaps backends on live pipelines without a reconnect |

Sections that exist in the dataclass tree but are not in this list (`server`,
`stt`, `tts`, `llm`, `audio`, `tools`, `memory`, `database`, `billing`,
`channel_gateway`, `coredump_scraper`) are all created with their defaults even
when absent from the YAML — you only declare what you want to change.

Unknown keys in any section are silently dropped (`_dict_to_dataclass` filters
to known fields), so a stale field name fails quietly rather than crashing. A
bad **value** (e.g. an invalid backend name) fails fast: `load_config` runs
`VoiceConfig.validate()` at the end and raises `ValueError` so the
`tinkerclaw-voice` unit restart-loops with an actionable cause.

## `server`

| Field | Type | Default | Description |
|---|---|---|---|
| `host` | str | `"0.0.0.0"` | Bind address for the voice server (WS + REST). |
| `port` | int | `3502` | Listen port. Overridable with `--port`. |
| `api_token` | str | `""` | Bearer token protecting every REST route except the public prefixes. Blank in the committed file — populated at load time from `DRAGON_API_TOKEN`. |

`api_token` is never written to the repo. Set it through the `DRAGON_API_TOKEN`
env var (shorter than `DRAGON_VOICE_SERVER_API_TOKEN`, and explicit env wins
over YAML). `GET /api/config` redacts it.

## `stt`

| Field | Type | Default | Description |
|---|---|---|---|
| `backend` | str | `"whisper_cpp"` | STT engine. One of `moonshine`, `whisper_cpp`, `vosk`, `openrouter`. |
| `model` | str | `"tiny"` | Engine model id (e.g. Whisper `tiny`/`medium`). |
| `language` | str | `"en"` | Transcription language code. |
| `moonshine_model_path` | str | `""` | Custom Moonshine model dir. Empty = auto-download on first use. |
| `whisper_model_path` | str | `""` | Custom whisper.cpp model path. |
| `vosk_model_path` | str | `""` | Custom Vosk model path. |
| `openrouter_api_key` | str | `""` | Cloud STT key. Auto-populated from `llm.openrouter_api_key` when `backend: openrouter` and left blank. |
| `openrouter_url` | str | `"https://openrouter.ai/api/v1"` | OpenRouter base URL for cloud STT. |
| `transcribe_backend` | str | `""` | Dedicated backend for the `POST /api/v1/transcribe` REST endpoint (long-form dictation). When set, batched WAV uploads use this while the WS pipeline keeps its low-latency `backend`. |
| `transcribe_model` | str | `""` | Model for `transcribe_backend`. |
| `transcribe_audio_dir` | str | `""` | Where `/api/v1/transcribe` preserves raw WAV uploads for later re-transcription. Empty = `/home/radxa/tinkerclaw/dictation_audio`. |

Valid `backend` (and `transcribe_backend`) values are validated at load:
`moonshine`, `whisper_cpp`, `vosk`, `openrouter`. The cloud OpenRouter STT
backend uses the `openai/gpt-audio-mini` model and sends base64-encoded WAV.

## `tts`

| Field | Type | Default | Description |
|---|---|---|---|
| `backend` | str | `"piper"` | TTS engine. One of `piper`, `kokoro`, `edge_tts`, `openrouter`, `neutts_air`, `supertonic`, `kitten`. |
| `piper_model` | str | `"en_US-lessac-medium"` | Piper voice model id. |
| `piper_data_dir` | str | `""` | Piper model data dir. Empty = default. |
| `kokoro_model_path` | str | `""` | Kokoro ONNX model path. |
| `kokoro_voices_path` | str | `""` | Kokoro voices `.bin` (kokoro-onnx 0.5.0+ needs it separately). |
| `kokoro_voice` | str | `"af_bella"` | Kokoro voice id. |
| `text_cleaner_enabled` | bool | `true` | Strip markdown / bullets / code fences / emojis / bare URLs before synthesis so TTS doesn't read punctuation aloud. |
| `edge_voice` | str | `"en-US-AriaNeural"` | Microsoft Edge TTS voice. |
| `sample_rate` | int | `22050` | TTS engine output rate (Hz). Dragon resamples to 16 kHz before sending to Tab5. |
| `neutts_ref_audio` | str | `""` | NeuTTS Air voice-clone reference audio path. |
| `neutts_ref_text` | str | `""` | NeuTTS Air reference transcript. |
| `supertonic_voice` | str | `"F1"` | Supertonic-3 voice (`F1`–`F5` / `M1`–`M5`). 44.1 kHz output. |
| `supertonic_lang` | str | `"en"` | Supertonic-3 language. |
| `supertonic_speed` | float | `1.0` | Supertonic-3 speaking rate. |
| `supertonic_steps` | int | `8` | Supertonic-3 diffusion steps. |
| `kitten_voice` | str | `"expr-voice-2-f"` | KittenTTS voice (8 expression voices, 24 kHz, English-only). |
| `kitten_speed` | float | `1.0` | KittenTTS speaking rate. |
| `openrouter_api_key` | str | `""` | Cloud TTS key. Auto-populated from `llm.openrouter_api_key` when `backend: openrouter` and blank. |
| `openrouter_url` | str | `"https://openrouter.ai/api/v1"` | OpenRouter base URL for cloud TTS. |
| `openrouter_voice` | str | `"alloy"` | OpenRouter TTS voice. |

The cloud OpenRouter TTS backend uses `openai/gpt-audio-mini` and streams pcm16
over SSE at 24 kHz (resampled to 16 kHz before sending to Tab5).

## `llm`

The largest section. Selects the language-model backend and carries
per-backend settings for all of `ollama`, `openrouter`, `lmstudio`,
`npu_genie`, `tinkerclaw`, the `dual` picker/responder pipeline, and the
`router` fleet.

| Field | Type | Default | Description |
|---|---|---|---|
| `backend` | str | `"ollama"` | Active LLM backend. One of `ollama`, `openrouter`, `lmstudio`, `npu_genie`, `tinkerclaw`, `dual`, `router`. |
| `local_backend` | str | `""` | Remembers the original local backend so cloud-mode fallback can revert. Set at load time to `backend` if left blank. |
| `ollama_url` | str | `"http://localhost:11434"` | Ollama base URL. |
| `ollama_model` | str | `"gemma3:4b"` | Ollama model id. See [model selection](#llm-model-selection) below. |
| `ollama_keep_alive` | str | `"30s"` | How long Ollama keeps a model resident after the last request. `dual` setups override to `"5m"` so picker + responder stay warm. |
| `openrouter_api_key` | str | `""` | OpenRouter key. Reads from `OPENROUTER_API_KEY` env when blank. Required when `backend: openrouter`. |
| `openrouter_model` | str | `"anthropic/claude-3-haiku"` | Cloud model id (user-selectable via the `llm_model` field in `config_update`). |
| `openrouter_url` | str | `"https://openrouter.ai/api/v1"` | OpenRouter base URL. |
| `lmstudio_url` | str | `"http://localhost:1234/v1"` | LM Studio / llama-server OpenAI-compatible endpoint. |
| `lmstudio_model` | str | `"default"` | LM Studio model id (`"default"` = whatever GGUF llama-server reports loaded). |
| `native_tools` | bool | `false` | Pass `tools=[...]` + `tool_choice="auto"` to the llama-server OpenAI API (requires `--jinja`) and consume structured `tool_calls`, instead of prose-listing tools and parsing XML markers. Off by default; the prose path is the fallback for models without native tool support. |
| `genie_model_dir` | str | `"/home/radxa/qairt/models/llama32-1b"` | NPU Genie (QAIRT) model directory. |
| `genie_config` | str | `"htp-model-config-llama32-1b-gqa.json"` | NPU Genie HTP model config file. |
| `system_prompt` | str | (Tinker base prompt) | Default system prompt. Overridden per voice mode by the mode-aware prompts. |
| `max_tokens` | int | `128` | Default generation cap. Overridden per voice mode (`MAX_TOKENS_LOCAL=1024`, `HYBRID=256`, `CLOUD=512`). Validated `1 ≤ n ≤ 4096`. |
| `temperature` | float | `0.7` | Sampling temperature. Validated `0 ≤ t ≤ 2`. |
| `tinkerclaw_url` | str | `"http://localhost:18789"` | TinkerClaw gateway URL (voice mode 3). |
| `tinkerclaw_token` | str | `""` | Gateway auth token. Blank in the committed file — injected from `TINKERCLAW_TOKEN` env. |
| `tinkerclaw_model` | str | `"minimax/MiniMax-M2.5"` | Model the TinkerClaw gateway uses. |
| `dual_picker_backend` | str | `""` | `backend: dual` — tool-picker backend. Empty = `ollama`. |
| `dual_picker_model` | str | `"hf.co/Salesforce/xLAM-2-1b-fc-r-gguf:Q4_K_M"` | Fast tool-picker model. |
| `dual_responder_backend` | str | `""` | `backend: dual` — responder backend. Empty = `ollama`. |
| `dual_responder_model` | str | `"ministral-3:3b"` | Warm responder model. |
| `fleet` | list[dict] | `[]` | `backend: router` — the per-turn model pool. Empty list = legacy single-backend dispatch. See [fleet](#fleet). |

### LLM model selection

For Local mode, the live default is **LFM2.5-VL-1.6B** served by llama-server
(`backend: lmstudio`, `lmstudio_url: http://localhost:1234/v1`), with
`ministral-3:3b` kept as a one-line Ollama rollback. The Ollama default in the
committed dataclass is `gemma3:4b`. Latency, accuracy gauntlets, and the
"when to use what" matrix live in [`docs/CHANGELOG.md`](../CHANGELOG.md).

To force pure Ollama (no llama-server probe):

```yaml
llm:
  backend: "ollama"
  local_backend: "ollama"
  ollama_model: "ministral-3:3b"
```

To run Local via llama-server (LM Studio-compatible):

```yaml
llm:
  backend: "lmstudio"
  local_backend: "lmstudio"
  lmstudio_url: "http://localhost:1234/v1"
  lmstudio_model: "default"
```

### `native_tools`

When `true`, Dragon uses the model's structured function-calling API instead of
the prose-list-and-parse path. It requires a llama-server started with
`--jinja`. A live A/B on LFM2.5-VL-1.6B lifted the hard-gauntlet score from
7/10 to 9/10. Leave it `false` for backends or models without native tool
support — the marker-parsing fallback handles those.

### `fleet`

Activate the multi-model router by setting `backend: "router"` and populating
`fleet`. Each entry mirrors a `ModelSpec`:

| Key | Type | Required | Description |
|---|---|---|---|
| `id` | str | yes | Stable identifier for logs / `fleet_summary`. |
| `backend` | str | yes | `ollama`, `openrouter`, `lmstudio`, `npu_genie`, or `tinkerclaw`. |
| `model_id` | str | yes | Backend-specific model id. |
| `caps` | list[str] | yes | Modalities: `text`, `vision`, `video`, `audio_in`, `audio_out`, `tool_calling`. |
| `tier` | str | yes | `local`, `cloud`, or `lan`. The active voice mode picks eligible tiers. |
| `priority` | int | yes | Lower wins among candidates that satisfy the required caps. |
| `keep_alive_s` | int | no | Ollama keep-alive seconds for this model. |
| `lmstudio_url` | str | no | Per-entry LM Studio URL (LAN-tier workstation). |

The router picks per-turn from the modalities present in the message and the
tier allowed by the voice mode. Copy-paste fleets and the full routing rule are
in the [router cookbook](../router-cookbook.md).

```yaml
llm:
  backend: "router"
  fleet:
    - {id: ministral,   backend: ollama, model_id: "ministral-3:3b",
       caps: [text, tool_calling],         tier: local, priority: 0,  keep_alive_s: 600}
    - {id: minicpm_v4,  backend: ollama,
       model_id: "hf.co/openbmb/MiniCPM-V-4-gguf:Q4_K_M",
       caps: [text, vision, video],        tier: local, priority: 10, keep_alive_s: 120}
    - {id: ds_v4_flash, backend: openrouter, model_id: "deepseek/deepseek-v4-flash",
       caps: [text, tool_calling],         tier: cloud, priority: 0}
    - {id: qwen36_flash, backend: openrouter, model_id: "qwen/qwen3.6-flash",
       caps: [text, vision, tool_calling], tier: cloud, priority: 5}
```

## `audio`

| Field | Type | Default | Description |
|---|---|---|---|
| `input_sample_rate` | int | `16000` | Tab5 mic PCM rate (Hz). |
| `input_channels` | int | `1` | Mic channels (mono). |
| `output_sample_rate` | int | `22050` | TTS engine native rate; resampled to 16 kHz before sending to Tab5. |
| `vad_enabled` | bool | `true` | Server-side energy-based silence detection. Set `false` when Tab5 sends explicit `start`/`stop`. |
| `vad_silence_ms` | int | `1500` | Milliseconds of silence before STT triggers. Raise for slower speakers. |

## `tools`

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Master switch for the agentic tool-calling pipeline. |
| `max_tool_calls` | int | `3` | Maximum tool invocations per turn (loop guard). |
| `web_search_engine` | str | `"duckduckgo"` | Web-search backend for the `web_search` tool. |
| `searxng_url` | str | `""` | Set to `http://your-searxng:8888` to use the self-hosted SearXNG metasearch (falls back to DuckDuckGo when blank or down). |

## `memory`

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Master switch for the memory + document RAG service. |
| `embed_model` | str | `"nomic-embed-text"` | Ollama embedding model (768-dim vectors). |
| `auto_extract_facts` | bool | `true` | Auto-extract facts from conversation for later recall. |
| `max_context_facts` | int | `3` | Facts injected into the system prompt per turn. |
| `max_context_chunks` | int | `3` | Document chunks injected per turn. |
| `max_document_bytes` | int | `10485760` | Cap on a single ingested document (10 MB). `0` = disable the cap. |

## `database`

| Field | Type | Default | Description |
|---|---|---|---|
| `message_retention_days` | int | `30` | Purge messages older than this. `0` = never purge. |
| `paused_session_retention_days` | int | `30` | Auto-end `paused` sessions idle longer than this, then purge their messages. `0` = disable. |

## `channel_gateway`

OpenClaw gateway connector for third-party messaging channels (Telegram,
WhatsApp, Discord, …). Off by default — startup uses an in-process
`MockConnector` until you enable it.

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | When `true`, swap `MockConnector` for the real `GatewayConnector` so `channel_reply` frames forward to the platform. |
| `url` | str | `"ws://127.0.0.1:18789"` | Gateway WS-RPC endpoint (loopback-only). |
| `token` | str | `""` | Auth token. Blank = reuse `llm.tinkerclaw_token` (same gateway process). |
| `client_id` | str | `"gateway-client"` | Must match OpenClaw's `GATEWAY_CLIENT_IDS` enum. |

## `coredump_scraper`

Dragon-side poller that pulls Tab5 coredumps off flash and archives them. Off
by default.

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Set `true` plus a non-empty `targets` to opt in. |
| `poll_interval_s` | float | `60.0` | Seconds between full target sweeps. |
| `request_timeout_s` | float | `10.0` | Per-request HTTP timeout. |
| `save_dir` | str | `"/home/radxa/tinkerclaw/tab5-coredumps"` | Archive root. Default is inside the systemd `ReadWritePaths` whitelist. |
| `firmware_elf` | str | `""` | Path to the deployed firmware ELF for auto-symbolicate. Empty = archive raw bin only. |
| `targets` | list | `[]` | Tab5s to poll. Each entry: `device_id`, `host`, `port` (default `8080`), `token`. |

## `billing`

The `billing` section is built with defaults even though it is not in the
`load_config` section list — it is created from the `BillingConfig` default.

| Field | Type | Default | Description |
|---|---|---|---|
| `daily_cap_cents` | int | `0` | Server-side daily LLM budget cap. When `> 0` and today's `api_usage` total exceeds it, Dragon emits a `cap_downgrade` frame to Tab5. `0` = disabled. Set via `BUDGET_DAILY_CENTS` env (overrides the file). |

## Top-level flags

| Field | Type | Default | Description |
|---|---|---|---|
| `progress_bus_emit_legacy` | bool | `true` | Double-write migrated emitters (legacy ad-hoc event + new `progress` event) so unmodified Tab5 firmware keeps working. Not hot-reloaded; a flip needs a restart. |

## Environment variable overrides

Any section field is overridable with `DRAGON_VOICE_{SECTION}_{KEY}`. Type is
coerced from the existing value (bool / int / float / str):

```bash
export DRAGON_VOICE_STT_BACKEND=moonshine
export DRAGON_VOICE_LLM_BACKEND=lmstudio
export DRAGON_VOICE_TTS_BACKEND=piper
export DRAGON_VOICE_LLM_OPENROUTER_API_KEY=sk-or-v1-...
export DRAGON_VOICE_AUDIO_VAD_SILENCE_MS=800
export DRAGON_VOICE_LLM_MAX_TOKENS=512
```

Four special env vars bypass the `DRAGON_VOICE_*` prefix and take precedence:

| Env var | Sets | Notes |
|---|---|---|
| `DRAGON_VOICE_CONFIG` | the config file path | Point at a non-default `config.yaml`. |
| `DRAGON_API_TOKEN` | `server.api_token` | The REST bearer. Explicit env wins over YAML. |
| `TINKERCLAW_TOKEN` | `llm.tinkerclaw_token` | Keeps the gateway token out of the repo. |
| `BUDGET_DAILY_CENTS` | `billing.daily_cap_cents` | Lets oncall flip the cap without redeploying. |

Secrets (any field whose name contains `api_key`, `token`, `password`, or
`secret`) are redacted by `config_to_dict(redact_secrets=True)`, which backs
`GET /api/config`.

## Hot-reload at runtime

`POST /api/config` merges a partial config and re-initializes active pipeline
instances in place — no WebSocket reconnect:

```bash
curl -X POST http://192.168.70.242:3502/api/config \
  -H "Content-Type: application/json" \
  -d '{"llm": {"backend": "ollama", "ollama_model": "ministral-3:3b"}}'
# → {"status": "ok", "message": "Config updated, 1 pipelines reloaded", ...}
```

The three-tier voice-mode swap (`config_update` over the WebSocket) hot-swaps
STT/TTS/LLM backends per voice mode without touching `config.yaml`; see the
[WebSocket protocol reference](websocket-protocol.md).

## Examples

### Minimal Local-mode `config.yaml`

```yaml
server:
  host: "0.0.0.0"
  port: 3502

stt:
  backend: "moonshine"

tts:
  backend: "piper"
  piper_model: "en_US-lessac-medium"

llm:
  backend: "lmstudio"
  local_backend: "lmstudio"
  lmstudio_url: "http://localhost:1234/v1"
  lmstudio_model: "default"

audio:
  vad_enabled: true
  vad_silence_ms: 1500
```

### Cloud-mode `config.yaml`

```yaml
stt:
  backend: "openrouter"      # gpt-audio-mini; key auto-propagated from llm

tts:
  backend: "openrouter"
  openrouter_voice: "alloy"

llm:
  backend: "openrouter"
  openrouter_model: "anthropic/claude-sonnet-4.6"
  # openrouter_api_key left blank — supplied via OPENROUTER_API_KEY env
```

### Production secrets via env (do NOT put keys in the repo file)

```bash
export OPENROUTER_API_KEY="sk-or-v1-..."   # → llm.openrouter_api_key
export DRAGON_API_TOKEN="..."              # → server.api_token (REST bearer)
export TINKERCLAW_TOKEN="..."              # → llm.tinkerclaw_token
python3 -m dragon_voice
```
