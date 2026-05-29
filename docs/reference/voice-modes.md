---
audience: integrator
type: reference
prerequisites: none
last-verified: 2026-05-29
---
# Voice modes reference (backend mapping)

[Tab5](../../GLOSSARY.md) selects how a turn is processed by sending one `config_update` frame over the [WebSocket](../../GLOSSARY.md); [Dragon](../../GLOSSARY.md) reads the `voice_mode` integer and hot-swaps its STT, LLM, and TTS backends in place — no reconnection, no dropped session. This page is the lookup table for that mapping: which backend each mode binds for each pipeline stage, the per-mode system-prompt budget and pipeline timeout, the auto-fallback rules, and the per-connection config isolation that lets two Tab5s run different modes against one Dragon.

The wire shape of the `config_update` frame itself (both accepted forms, ACK latency, the Tab5-side-only modes) is in the [WebSocket protocol reference](websocket-protocol.md#config_update-inbound). What each mode *changes on Dragon* in operational terms is in [Swap the LLM backend](../how-to/swap-the-llm-backend.md). This page is the authoritative field index for the mapping.

## The four wire tiers

`config_update.voice_mode` is one of four integers Dragon accepts as live state. Each binds a fixed STT/TTS pairing; the LLM column is the per-mode default before the [router](../../GLOSSARY.md) (if active) and any `llm_model` override apply.

| `voice_mode` | Mode | STT | LLM | TTS |
|---|---|---|---|---|
| `0` | **Local** | Moonshine | Local (`lmstudio`/`ollama`/`npu_genie`) | Piper (22050 Hz) |
| `1` | **Hybrid** | OpenRouter `gpt-audio-mini` | Local (unchanged) | OpenRouter `gpt-audio-mini` (24 kHz) |
| `2` | **Full Cloud** | OpenRouter `gpt-audio-mini` | OpenRouter (user-selected `llm_model`) | OpenRouter `gpt-audio-mini` (24 kHz) |
| `3` | **TinkerClaw** | Moonshine (or OpenRouter) | TinkerClaw gateway (agent runner) | Piper (or OpenRouter) |

Notes on the mapping:

- **Hybrid (1)** moves only STT and TTS to the cloud; the LLM stays Dragon-local. It is the cheapest path to a snappy assistant because the round-trip cost is audio I/O, not generation.
- **Full Cloud (2)** is the only mode that reads `llm_model`. Accepted values include `anthropic/claude-3-haiku`, `anthropic/claude-sonnet-4-20250514`, and `openai/gpt-4o-mini`; the value is stored in `LLMConfig.openrouter_model`.
- **TinkerClaw (3)** bypasses Dragon's `ConversationEngine`, `ToolRegistry`, and `MemoryService` entirely — Dragon is an audio pipe (STT in, TTS out) and the LLM call goes to the [TinkerClaw](../../GLOSSARY.md) gateway at `localhost:18789`. See [Run the TinkerClaw sidecar](../how-to/run-the-tinkerclaw-sidecar.md).

### Modes 4 and 5 (Tab5-side-only)

Modes 4 (TinkerON) and 5 (Solo) run the turn without Dragon. Tab5 **downconverts them to `voice_mode=0` on the wire**, so Dragon never sees them as live state — treat this as a protocol feature, not a bug. Dragon's mapping table therefore stops at mode 3.

### Backward compatibility: `cloud_mode`

Older firmware sends a boolean instead of an integer. Dragon still accepts it: `cloud_mode: true` maps to `voice_mode=2`, `cloud_mode: false` maps to `voice_mode=0`. The boolean form cannot express Hybrid (mode 1); new clients should send the integer form.

## Valid backend keys per stage

The mode table above picks from these registered backend keys. A backend is selected by its key string in `config.yaml` (`stt.backend`, `tts.backend`, `llm.backend`) or hot-swapped at runtime.

| Stage | Valid keys |
|---|---|
| STT | `moonshine`, `whisper_cpp`, `vosk`, `openrouter` |
| TTS | `piper`, `kokoro`, `edge_tts`, `openrouter` |
| LLM | `ollama`, `npu_genie`, `openrouter`, `lmstudio`, `tinkerclaw`, `dual`, `router` |

The `router` LLM key activates the [`CapabilityAwareRouter`](../../GLOSSARY.md), which holds a fleet of sub-backends and picks one per turn from the active mode's tier policy. See [Configure the multi-model router](../how-to/configure-the-multi-model-router.md) and the [router cookbook](../router-cookbook.md).

## Mode-aware system prompts

Each voice mode sets a different system-prompt budget so the local model's context stays tight while cloud models get room for nuance.

| Mode | System-prompt budget |
|---|---|
| Local (0) | Concise — 128 tokens |
| Hybrid (1) | Medium — 256 tokens |
| Cloud (2) | Rich — 512 tokens |

When `voice_mode` changes, the session's `system_prompt` is updated in the database immediately, so the `ConversationEngine` picks up the new prompt on the next turn rather than the next reconnect.

## Mode-aware pipeline timeouts

The pipeline applies a per-mode wall-clock timeout to a turn, configured in `pipeline.py`. Local models on the Q6A are slow, and tool-calling chains compound that, so Local gets the longest budget.

| Mode | Pipeline timeout | Why |
|---|---|---|
| Local (0) | 300 s (5 min) | Tool-calling chains on slow local models |
| Cloud (2) | 60 s (1 min) | Cloud generation is fast |
| TinkerClaw (3) | 180 s (3 min) | Gaps between gateway tool executions |

These are the pipeline-level turn timeouts. They are distinct from the local LLM backend's HTTP timeouts: `lmstudio_llm.py` uses `ClientTimeout(total=600, sock_read=300)` because individual Local-mode turns on the Q6A routinely hit 90–180 s, and `MAX_TOKENS_LOCAL = 1024` so thinking-mode models that spend budget inside `<think>...</think>` still emit visible content.

## Auto-fallback

Cloud STT/TTS failures degrade gracefully to local for the failed request, then push Tab5 back to Local mode for subsequent turns.

| Trigger | Per-request behavior | Mode-level behavior |
|---|---|---|
| Cloud STT/TTS fails (timeout, API error) | Falls back to local `moonshine`/`piper` for that request | Sends a `config_update` with an `error` field → Tab5 auto-reverts to Local (mode 0) |
| TinkerClaw gateway down (connection refused on `localhost:18789`) | Sends `error` to Tab5 | Same auto-revert-to-Local pattern as cloud fallback |
| `lmstudio` socket unreachable at process start | `create_llm()` transparently falls back to Ollama for the session | One-shot at backend creation, **not per-request** — warm-path latency is unaffected |

The API key needed for cloud fallback is auto-propagated: `llm.openrouter_api_key` is copied to `stt.openrouter_api_key` and `tts.openrouter_api_key`, and is validated before a swap — a cloud swap with an empty key is rejected with an error rather than silently failing mid-turn.

The `lmstudio`→Ollama fallback is evaluated only once, when the backend instance is created. Restarting `tinkerclaw-llama-server` mid-session is **not** picked up automatically — after fixing llama-server, also run `systemctl restart tinkerclaw-voice` so the backend is re-evaluated.

## Per-connection config isolation (deep copy)

Each WebSocket connection gets its own deep copy of the global config via `copy.deepcopy()`. A `config_update` from one device — say, switching to Cloud — mutates only that connection's pipeline config, never the shared global object or another device's pipeline.

Without the deep copy, two Tab5s connected at once would share one mutable config object and one device's mode switch would corrupt the other's pipeline. With it, two devices can hold different modes against the same Dragon simultaneously.

## Reconnect behavior

When a device reconnects, the pipeline is re-initialized with **Local defaults (`voice_mode=0`)** regardless of the previous session's mode. The session history is preserved (the session resumes), but the mode is not. A client that wants to stay in Cloud or Hybrid must re-send its `config_update` after the `session_start` frame arrives.

## Config fields

| Field | Type | Role |
|---|---|---|
| `LLMConfig.backend` | str | Active LLM backend key for the current mode |
| `LLMConfig.local_backend` | str | Remembers the original local backend for fallback |
| `LLMConfig.openrouter_model` | str | User-selectable cloud model (`llm_model` from `config_update`) |

## Examples

### Switch to Full Cloud with a specific model

```json
{"type": "config_update", "voice_mode": 2, "llm_model": "anthropic/claude-sonnet-4-20250514"}
```

Dragon swaps STT and TTS to OpenRouter `gpt-audio-mini`, sets the LLM to OpenRouter with the named model, raises the system-prompt budget to 512 tokens, and applies the 60 s pipeline timeout. It replies with a `config_update` ACK reporting the applied backends.

### Switch back to Local (legacy boolean form)

```json
{"type": "config_update", "cloud_mode": false}
```

Maps to `voice_mode=0`: STT → Moonshine, TTS → Piper (22050 Hz), LLM → the local backend, system prompt back to 128 tokens, pipeline timeout back to 300 s.

### Hot-reload the LLM backend over REST (no Tab5)

```bash
curl -X POST http://192.168.70.242:3502/api/config \
  -H "Content-Type: application/json" \
  -d '{"llm": {"backend": "ollama", "ollama_model": "ministral-3:3b"}}'
# → {"status": "ok", "message": "Config updated, 1 pipelines reloaded", ...}
```

This swaps the backend on every active pipeline in place, the same hot-swap path a `config_update` frame triggers.

## See also

- [WebSocket protocol reference](websocket-protocol.md) — the `config_update` frame shape, ACK latency, and the full inbound/outbound frame index.
- [Swap the LLM backend](../how-to/swap-the-llm-backend.md) — operational walkthrough of what each mode changes on Dragon.
- [Configure the multi-model router](../how-to/configure-the-multi-model-router.md) — populating `fleet` and reading `fleet_summary` when `llm == "router"`.
- [Run the TinkerClaw sidecar](../how-to/run-the-tinkerclaw-sidecar.md) — enabling voice mode 3.
- [Router cookbook](../router-cookbook.md) — copy-paste fleets for common routing patterns.
- [`docs/protocol.md`](../protocol.md) — the canonical full wire specification.
