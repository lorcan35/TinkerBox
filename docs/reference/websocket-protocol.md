---
audience: integrator
type: reference
prerequisites: none
last-verified: 2026-05-29
---
# WebSocket protocol (Dragon side)

The single WebSocket between [Tab5](../../GLOSSARY.md) (the face) and [Dragon](../../GLOSSARY.md) (the brain) carries every voice turn, text turn, control command, tool event, rich-media push, and call frame. This page is the lookup table for that wire from Dragon's side: every frame type, its direction, its fields, and when it fires.

The full narrative specification — sequence diagrams, state machine, audio-pipeline internals, OTA, widget platform, channel messaging, the progress event bus — is [`docs/protocol.md`](../protocol.md). That document is canonical; this reference is the condensed field index. When the two disagree, `docs/protocol.md` wins.

## Endpoint

| Property | Value |
|---|---|
| URL | `ws://<dragon-ip>:3502/ws/voice` |
| Transport | WebSocket (RFC 6455) |
| Port | `3502` (`tinkerclaw-voice` service) |
| Path | `/ws/voice` |
| Max message size | 10 MB (server limit) |
| Server heartbeat | 600 s (aiohttp) |
| First frame | `register` (JSON text) — Dragon rejects all other commands until registration is processed |

Frames are either **JSON text frames** (every control and event message; each carries a `type` field) or **binary frames** (PCM audio, plus the `VID0`/`AUD0`-tagged call frames). Dragon disambiguates binary frames by their leading 4-byte magic tag — an untagged binary frame is mic PCM bound for STT.

## Inbound frames (Tab5 → Dragon)

| Type | Frame | Key fields | Fires when | Dragon does |
|---|---|---|---|---|
| `register` | JSON | `device_id` (req), `hardware_id` (req), `firmware_ver` (req), `platform` (req), `name`, `session_id`, `capabilities` | First frame after connect; once per connection | Upserts device, creates/resumes session, inits pipeline, replies `session_start` |
| `start` | JSON | `mode` (`"ask"` default, or `"dictate"`) | Push-to-talk / dictation begins | Clears audio buffer, sets mode, awaits binary PCM |
| (binary PCM) | Binary | 640-byte chunks (20 ms, 16 kHz mono int16) | Continuously during `LISTENING` | Appends to audio buffer; server VAD may auto-trigger |
| `segment` | JSON | — | Tab5 detects a 500 ms pause in dictation mode | Transcribes the segment, replies `stt_partial` |
| `stop` | JSON | — | Push-to-talk released / 30 s ask cap / 5 s dictation silence | Ask: STT → LLM → TTS. Dictate: finalize combined transcript |
| `cancel` | JSON | — | User cancels, or Tab5's 35 s response timeout fires | Sets cancel flag, kills in-flight STT/LLM/TTS task, clears buffers |
| `text` | JSON | `content` (req, non-empty) | User types or sends text via API | Skips STT, streams `llm` tokens; TTS per `response_mode` |
| `clear` | JSON | — | User clears history | Ends current session, creates a fresh one, replies `session_start` |
| `config_update` | JSON | `cloud_mode` (bool) **or** `voice_mode` (int) + `llm_model` | User toggles mode in Settings | Hot-swaps STT/TTS/LLM backends in place, replies `config_update` ACK |
| `config_ack` | JSON | `applied` | Tab5 confirms receipt of a Dragon `config_update` | Logs at debug level only |
| `ready_ack` | JSON | `mode` (int) | Tab5's playback ring drained and the orb returned to `READY` | Logs the event; no state change |
| `ping` | JSON | — | Every 15 s during `PROCESSING`/`SPEAKING` | Replies `pong` immediately |
| `user_media` | JSON | `media_id` (req), `media_type` (`"image"`), `text` | Camera photo uploaded via `POST /api/media/upload` | Loads image from MediaStore, runs multimodal LLM, streams reply |
| `widget_action` | JSON | `card_id` (req), `event` (req), `payload` | User taps a widget action or answers a prompt | `widget_action_router` dispatches to the owning skill's `on_action` |
| `channel_reply` | JSON | `channel`, `thread_id`, `text`, `in_reply_to` | User replies to a `channel_message` | Forwards via `GatewayConnector` to OpenClaw, replies `channel_reply_ack` |

### `register`

The mandatory first frame. Sent once per connection.

```json
{
  "type": "register",
  "device_id": "aabbccddeeff",
  "hardware_id": "AA:BB:CC:DD:EE:FF",
  "name": "Tab5",
  "firmware_ver": "0.5.0",
  "platform": "esp32p4-tab5",
  "session_id": null,
  "capabilities": {
    "mic": true, "speaker": true, "screen": true,
    "camera": true, "sd_card": true, "touch": true,
    "widgets": {
      "types": ["live", "card", "list", "chart", "media", "prompt"],
      "list_max_items": 5, "chart_max_points": 12, "prompt_max_choices": 3,
      "screen_w": 720, "screen_h": 1280,
      "media_max_w": 660, "media_max_h": 440, "action_rate_per_sec": 4
    }
  }
}
```

Pass a non-null `session_id` to request resume of a previously paused session. Widget renderer limits are nested under `capabilities.widgets` — there is no separate `widget_capability` frame.

### `config_update` (inbound)

Two accepted shapes. New clients should use the integer form; the boolean form cannot express Hybrid (`voice_mode=1`) and is kept only for older firmware.

```json
{"type": "config_update", "cloud_mode": true}
```

```json
{"type": "config_update", "voice_mode": 0, "llm_model": "anthropic/claude-3-haiku"}
```

| `voice_mode` | Mode | STT | LLM | TTS |
|---|---|---|---|---|
| `0` | Local | Moonshine | Local (lmstudio/ollama) | Piper (22050 Hz) |
| `1` | Hybrid | OpenRouter gpt-audio-mini | Local (unchanged) | OpenRouter gpt-audio-mini (24 kHz) |
| `2` | Full Cloud | OpenRouter gpt-audio-mini | OpenRouter (`llm_model`) | OpenRouter gpt-audio-mini (24 kHz) |
| `3` | TinkerClaw | Moonshine / OpenRouter | TinkerClaw gateway | Piper / OpenRouter |

`cloud_mode: true` maps to `voice_mode=2`, `false` to `voice_mode=0`. Modes 4 (TinkerON) and 5 (Solo) are Tab5-side-only — Tab5 downconverts them to `voice_mode=0` on the wire, so Dragon never sees them as live state. Rapid `config_update` sends are coalesced server-side; expect 500–1000 ms ACK latency under back-pressure. See [Swap the LLM backend](../how-to/swap-the-llm-backend.md) for what each mode changes on Dragon.

### `text`

```json
{"type": "text", "content": "What time is it?"}
```

No `stt` frame follows text input — the flow starts directly at `llm` tokens. Whether TTS audio follows depends on the session's `response_mode` (`"always_speak"` synthesizes audio; `"match_input"` returns text-only for a text turn).

### `user_media`

```json
{"type": "user_media", "media_id": "m3n4o5p6", "media_type": "image", "text": "What is this plant?"}
```

`media_id` comes from a prior `POST /api/media/upload`. The reply streams as a normal turn (`llm` → `llm_done` → optional TTS).

### `channel_reply`

```json
{"type": "channel_reply", "channel": "tg", "thread_id": "tg:8675309",
 "text": "yes lunch thurs works, 12:30 ok?", "in_reply_to": "tg:8675309:42"}
```

Mirrors `channel` and `thread_id` from the inbound `channel_message`. Dragon forwards through the OpenClaw gateway and returns `channel_reply_ack`.

## Outbound frames (Dragon → Tab5)

| Type | Frame | Key fields | Fires when | Tab5 does |
|---|---|---|---|---|
| `session_start` | JSON | `session_id`, `device_id`, `resumed`, `message_count`, `config` | After `register`/`clear` once pipeline is ready | Stores `session_id` in NVS, transitions to `READY` |
| `stt` | JSON | `text`, `stt_ms` | After STT of the full utterance | Stores transcript; stays `PROCESSING` (ask) or → `READY` (dictate) |
| `stt_partial` | JSON | `text`, `stt_ms` | After each dictation segment | Appends to running transcript, stays `LISTENING` |
| `llm` | JSON | `text` | Per token as the LLM streams | Appends to LLM buffer, updates UI |
| `llm_done` | JSON | `llm_ms` | LLM finished all tokens | Logs timing |
| `tts_start` | JSON | — | Before the first binary TTS chunk | Enables speaker, → `SPEAKING` |
| (binary TTS) | Binary | 4096-byte chunks (16 kHz mono int16) | Between `tts_start` and `tts_end` | Upsamples 16k→48k, writes ring buffer |
| `tts_end` | JSON | `tts_ms` | After all TTS audio sent | Drains buffer, disables speaker, → `READY` |
| `dictation_summary` | JSON | `title`, `summary` | Async after dictation (transcript > 20 chars) | Stores title/summary, updates UI |
| `tool_call` | JSON | `tool`, `args` | LLM emitted tool markers, before execution | Shows tool activity indicator, stays `PROCESSING` |
| `tool_result` | JSON | `tool`, `result`, `execution_ms` | After tool execution, before LLM resumes | Updates indicator, stays `PROCESSING` |
| `media` | JSON | `media_type`, `url`, `width`, `height`, `alt` | After `llm_done`, code/table/image rendered | Fetches and displays inline |
| `card` | JSON | `title`, `subtitle`, `image_url`, `description` | Structured content in the response | Renders a styled card |
| `audio_clip` | JSON | `url`, `duration_s`, `label` | Non-TTS audio in the response | Shows a tap-to-play audio widget |
| `text_update` | JSON | `text` | After media render, code stripped (before `media`) | Replaces (or removes, if `""`) the last AI bubble |
| `config_update` | JSON | `config` (`cloud_mode`, `stt`, `tts`, `llm`) | After a config change, or pushed unprompted | Persists `cloud_mode` to NVS |
| `error` | JSON | `code`, `message` | On any pipeline error | Stops playback, → `READY` (connected) or `IDLE` |
| `pong` | JSON | — | In response to `ping` | No action (debug log) |
| `channel_message` | JSON | `channel`, `message_id`, `thread_id`, `sender`, `text`, `preview`, `priority`, `needs_reply` | Third-party platform message via OpenClaw | Routes to toast or now-card; fires audio cue |
| `channel_reply_ack` | JSON | `channel`, `thread_id`, `ok`, `platform_message_id`, `error` | After a `channel_reply` is processed | Toast "Replied via {channel}" or error toast |

There is also a family of `widget_*` push frames (`widget_live`, `widget_live_update`, `widget_live_dismiss`, `widget_card`, `widget_list`, `widget_chart`, `widget_media`, `widget_prompt`, `widget_dismiss`) and a unified `progress` frame. They are catalogued in [`docs/protocol.md`](../protocol.md) §17 and §19; this page covers the core voice/text/media surface the task lists.

### `session_start`

```json
{
  "type": "session_start",
  "session_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "device_id": "aabbccddeeff",
  "resumed": false,
  "message_count": 0,
  "config": {
    "stt": "moonshine",
    "tts": "piper",
    "llm": "router",
    "tts_sample_rate": 22050,
    "response_mode": "match_input",
    "system_prompt": "You are Tinker...",
    "fleet_summary": {
      "text":         "ministral-3:3b",
      "vision":       "hf.co/openbmb/MiniCPM-V-4-gguf:Q4_K_M",
      "video":        "hf.co/openbmb/MiniCPM-V-4-gguf:Q4_K_M",
      "audio_in":     null,
      "audio_out":    null,
      "tool_calling": "ministral-3:3b"
    }
  }
}
```

`resumed` is `true` with `message_count > 0` when a paused session was rejoined. `config.fleet_summary` is present only when `llm == "router"`; it reports the per-modality `model_id` the router would currently pick at the active tier, and is re-sent on every `config_update` ACK after a `voice_mode` change. Firmware that does not understand it can ignore it. See [Configure the multi-model router](../how-to/configure-the-multi-model-router.md).

### `error`

```json
{"type": "error", "code": "stt_failed", "message": "Transcription failed: model not loaded"}
```

| `code` | Meaning |
|---|---|
| `session_invalid` | Device not registered, or session not found |
| `internal` | Pipeline init or internal server error |
| `stt_failed` | STT transcription failed |
| `llm_failed` | LLM text processing failed |
| `tts_failed` | TTS synthesis failed |

When STT returns empty text, Dragon sends an `error` and stops — no `llm`/`tts` follows. On any `error`, Tab5 transitions to `READY` if the socket is still up (transient), or `IDLE` if it disconnected.

### `tool_call` / `tool_result`

These interleave with `llm` tokens during the response. The LLM generates twice — once to emit the tool markers, then again with the tool result injected — so a tool turn produces tokens, the call/result pair, then the final token stream.

```json
{"type": "tool_call", "tool": "web_search", "args": {"query": "weather in Dublin today"}}
```

```json
{"type": "tool_result", "tool": "web_search", "result": {"snippets": ["Dublin: 14°C, partly cloudy..."]}, "execution_ms": 234}
```

Dragon caps tool calls at **3 per turn** to prevent infinite loops; each call produces its own `tool_call` + `tool_result` pair before the final answer. See [Add a tool](../how-to/add-a-tool.md) for the registry and dialect parser.

### `text_update`

```json
{"type": "text_update", "text": "Here is the solution:\n\n(see image above)\n\nLet me know if you need changes."}
```

**Ordering is load-bearing.** `text_update` is emitted *before* any `media` frame for the same turn, because Tab5 targets the tail of the chat store when replacing a bubble and the `media` event appends a new bubble to that tail. An empty `text` (`""`) means the whole response was a code block that moved into media — Tab5 removes the most recent AI bubble entirely. The contract is enforced by `tests/audit/test_d5_d6_ws.py`, which asserts `text_update.index < media.index` against a live Dragon.

## Binary call frames (VID0 / AUD0)

Two-way video calls (Tab5 ↔ Dragon ↔ Tab5 or browser) use tagged binary frames. Dragon is a verbatim broadcast relay — a frame from one connection is forwarded byte-for-byte to all *other* connections, with no transcode and no buffering beyond the WS write queue (`dragon_voice/video_upstream.py`).

| Magic (4 bytes ASCII) | Length (4 bytes BE u32) | Payload |
|---|---|---|
| `VID0` | `len` | JPEG frame (Tab5 HW encoder, browser `getUserMedia`, or `POST /api/video/inject`) |
| `AUD0` | `len` | Raw 16 kHz mono int16 PCM (Tab5 mic in call mode, or browser Web Audio capture) |

An untagged binary frame is not a call frame — it is mic PCM bound for STT (the standard voice path). The magic tag is the only thing that disambiguates the two binary uses on the same socket.

## Audio format

| Direction | Encoding | Rate | Channels | Chunk |
|---|---|---|---|---|
| Tab5 → Dragon (mic) | PCM int16 LE | 16000 Hz | 1 (mono) | 640 bytes (20 ms) |
| Dragon → Tab5 (TTS) | PCM int16 LE | 16000 Hz | 1 (mono) | 4096 bytes |

Dragon resamples TTS from the engine's native rate (Piper 22050 Hz, OpenRouter 24 kHz) down to 16 kHz before sending. The first 4 TTS chunks ship immediately to pre-fill Tab5's ring buffer; the rest are paced at ~80% of real-time playback to keep the buffer from overflowing.

## See also

- [`docs/protocol.md`](../protocol.md) — the canonical full specification (sequence diagrams, state machine, OTA, widgets, progress bus, channel messaging).
- [How the stack fits together](../explanation/how-the-stack-fits-together.md) — why Tab5 is thin and Dragon holds all intelligence.
- [Swap the LLM backend](../how-to/swap-the-llm-backend.md) — what each `voice_mode` changes on Dragon.
- [Configure the multi-model router](../how-to/configure-the-multi-model-router.md) — populating `fleet` and reading `fleet_summary`.
- [Add a tool](../how-to/add-a-tool.md) — the tool registry behind `tool_call` / `tool_result`.
