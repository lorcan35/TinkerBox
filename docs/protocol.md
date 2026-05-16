# TinkerClaw WebSocket Protocol

**Version:** 2.0.0
**Date:** 2026-04-02
**Status:** Implemented -- both repos implement against this spec.

This document defines the WebSocket protocol between **Tab5** (ESP32-P4 thin client, TinkerTab repo) and **Dragon** (Python voice server, TinkerBox repo). Tab5 is the face. Dragon is the brain.

**Reference implementations:**
- Client: `TinkerTab/main/voice.c` (C, ESP-IDF)
- Server: `TinkerBox/dragon_voice/server.py` + `pipeline.py` (Python, aiohttp)

---

## Table of Contents

1. [Connection](#1-connection)
2. [Registration](#2-registration)
3. [Voice Ask Flow](#3-voice-ask-flow)
4. [Voice Dictation Flow](#4-voice-dictation-flow)
5. [Text Input Flow](#5-text-input-flow)
6. [Control Messages](#6-control-messages)
7. [Session Management](#7-session-management)
8. [Error Handling](#8-error-handling)
9. [Audio Format](#9-audio-format)
10. [State Machine](#10-state-machine)
11. [Dictation Post-Processing](#11-dictation-post-processing)
12. [Tool Execution Events](#12-tool-execution-events)
13. [OTA Protocol](#13-ota-protocol)
14. [config_update Backward Compatibility](#14-config_update-backward-compatibility)
15. [Rich Media Messages](#15-rich-media-messages)
16. [Message Reference](#16-message-reference)
17. [Widget Platform Messages](#17-widget-platform-messages-v1-april-2026)
18. [Video + Call Audio](#18-video--call-audio-april-2026)
19. [Progress Event Bus](#19-progress-event-bus-β-arch-april-2026)

---

## 1. Connection

### WebSocket Endpoint

```
ws://<dragon-ip>:3502/ws/voice
```

| Parameter | Value |
|-----------|-------|
| Transport | WebSocket (RFC 6455) |
| Port | 3502 |
| Path | `/ws/voice` |
| Max message size | 10 MB (server-side limit) |
| Heartbeat | 600s (aiohttp server-side) |
| Connect timeout | 5000ms (Tab5 client-side) |

### Handshake Sequence

```
Tab5                                    Dragon
  |                                       |
  |--- HTTP GET /ws/voice             -->|   WebSocket upgrade
  |<-- 101 Switching Protocols        ---|
  |                                       |
  |--- register (JSON text frame)     -->|   MUST be first frame
  |<-- session_start (JSON text frame) --|   Pipeline ready
  |                                       |
  |  ... conversation ...                 |
```

Tab5 uses `esp_transport_ws` from ESP-IDF to establish the connection. The `register` message MUST be the first text frame sent after the WebSocket handshake completes. Dragon does not accept any other commands until registration is processed.

### Reconnection

Tab5 implements automatic reconnection with exponential backoff:

| Parameter | Value |
|-----------|-------|
| Base delay | 2000ms |
| Max delay | 15000ms |
| Strategy | Exponential backoff |

On reconnect, Tab5 sends the stored `session_id` in the `register` message to resume the previous session (see [Session Management](#7-session-management)).

---

## 2. Registration

### 2.1 register (Tab5 -> Dragon)

**MUST be the first text frame after WebSocket connect.** Sent exactly once per connection.

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
    "mic": true,
    "speaker": true,
    "screen": true,
    "camera": true,
    "sd_card": true,
    "touch": true,
    "widgets": {
      "types": ["live","card","list","chart","media","prompt"],
      "list_max_items": 5,
      "chart_max_points": 12,
      "prompt_max_choices": 3,
      "screen_w": 720,
      "screen_h": 1280,
      "media_max_w": 660,
      "media_max_h": 440,
      "action_rate_per_sec": 4
    }
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | string | yes | Must be `"register"` |
| `device_id` | string | yes | Persistent device UUID from NVS. Generated once on first boot. |
| `hardware_id` | string | yes | MAC address or hardware serial. Immutable. |
| `name` | string | no | User-friendly device name (default `"Tab5"`). |
| `firmware_ver` | string | yes | Firmware version string (e.g. `"0.5.0"`). |
| `platform` | string | yes | Device type identifier (e.g. `"esp32p4-tab5"`). |
| `session_id` | string or null | no | Previous session ID to resume, or `null` for new session. |
| `capabilities` | object | yes | Declares device hardware capabilities. |
| `capabilities.widgets` | object | no | Wave-14 W14-H19: widget capability descriptor. Tab5 advertises widget-renderer limits here instead of as a separate `widget_capability` frame (see below). `types` is the set of widget kinds the renderer supports; the `*_max_*` / `screen_*` numbers let skills downgrade gracefully (e.g. a 20-point chart truncates to 12). |

**When sent:** Immediately after WebSocket connection is established, before any other frames.

**Expected response:** `session_start` from Dragon (see below).

**Dragon behavior on receive:**
1. Upserts the device record in the database (device_id, hardware_id, name, firmware_ver, platform, capabilities).
2. Logs a `device.connected` event.
3. Creates or resumes a session (see [Session Management](#7-session-management)).
4. Initializes the voice pipeline (STT, LLM, TTS backends).
5. Sends `session_start` response.

### 2.2 session_start (Dragon -> Tab5)

Sent immediately after registration is processed and the pipeline is fully initialized.

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

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"session_start"` |
| `session_id` | string | Assigned or resumed session ID. Tab5 stores this in NVS. |
| `device_id` | string | Echo of the registered device ID. |
| `resumed` | bool | `true` if this is a resumed session with existing history. |
| `message_count` | int | Number of messages in the resumed session (`0` for new). |
| `config` | object | Active backend configuration for this session. |
| `config.fleet_summary` | object\|null | **(#186, present only when `llm == "router"`)** Per-modality model_id the router would currently pick at the active voice_mode tier.  Keys are `Modality` values (text, vision, video, audio_in, audio_out, tool_calling). Value is the chosen model_id or `null` if no fleet entry has that capability in the current tier. Tab5 firmware can ignore this field — the legacy `vision_capability` event keeps firing for backward compat. New firmware can use it to light up dynamic capability chips. Re-sent on every `config_update` ACK after a voice_mode change. |

**When sent:** After Dragon finishes initializing the pipeline (may take several seconds on first connect as models load).

**Tab5 behavior on receive:**
1. Stores `session_id` in NVS for future session resume.
2. Transitions from `CONNECTING` to `READY` state.
3. The device is now ready to accept user interaction (push-to-talk, text input, etc.).

**IMPORTANT:** Tab5 does NOT transition to `READY` on WebSocket connect. It waits for `session_start` because Dragon's pipeline initialization (loading STT/TTS/LLM models) can take several seconds. Transitioning early would allow recording before the server is ready, causing lost audio.

---

## 3. Voice Ask Flow

Ask mode is the primary voice interaction: user speaks, Dragon transcribes, generates an LLM response, and speaks it back via TTS. Limited to 30 seconds of recording.

### Sequence Diagram

```
Tab5                                    Dragon
  |                                       |
  |--- {"type":"start"}              -->|   Clear audio buffer
  |--- [binary PCM frames]           -->|   20ms chunks, 640 bytes each
  |--- [binary PCM frames]           -->|   ...continues up to 30s
  |--- {"type":"stop"}               -->|   Process buffered audio
  |                                       |
  |<-- {"type":"stt","text":"..."}    ---|   Transcription result
  |<-- {"type":"llm","text":"It"}     ---|   Streaming LLM tokens
  |<-- {"type":"llm","text":"'s"}     ---|   ...one per token
  |<-- {"type":"llm","text":" sunny"} ---|
  |<-- {"type":"llm_done","llm_ms":N} ---|   LLM generation complete
  |<-- {"type":"tts_start"}           ---|   Audio stream begins
  |<-- [binary TTS audio]            ---|   4096-byte chunks
  |<-- [binary TTS audio]            ---|   ...paced at ~80% real-time
  |<-- {"type":"tts_end","tts_ms":N}  ---|   Audio stream complete
  |                                       |
```

### 3.1 start (Tab5 -> Dragon)

```json
{"type": "start"}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"start"` |

**When sent:** When user presses the push-to-talk button (or equivalent trigger).

**Dragon behavior:** Clears the audio buffer. Sets mode to `"ask"`. Ready to receive binary PCM frames.

**Tab5 behavior after sending:**
1. Clears previous transcript buffers (STT, LLM).
2. Spawns a mic capture task on core 1.
3. Transitions to `LISTENING` state.

### 3.2 Binary PCM Audio (Tab5 -> Dragon)

Raw binary WebSocket frames containing PCM audio data.

| Parameter | Value |
|-----------|-------|
| Frame type | Binary |
| Encoding | PCM signed 16-bit little-endian |
| Sample rate | 16000 Hz |
| Channels | 1 (mono) |
| Chunk duration | 20ms |
| Chunk size | 640 bytes (320 samples x 2 bytes) |
| Max duration | 30 seconds (1500 chunks) in Ask mode |

**When sent:** Continuously after `start`, every 20ms, until `stop` is sent or 30s limit reached.

**Dragon behavior:** Appends to the audio buffer. If server-side VAD is enabled, may auto-trigger processing on silence detection.

### 3.3 stop (Tab5 -> Dragon)

```json
{"type": "stop"}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"stop"` |

**When sent:** User releases push-to-talk button, or 30s recording limit reached (auto-stop).

**Dragon behavior in Ask mode:**
1. Takes the buffered audio.
2. Runs STT transcription.
3. Sends `stt` result.
4. Streams LLM response tokens.
5. Synthesizes TTS audio per sentence.
6. Sends `tts_start`, binary audio chunks, then `tts_end`.

**Tab5 behavior after sending:**
1. Stops mic capture task.
2. Resets the activity timestamp (starts the response timeout clock).
3. Transitions to `PROCESSING` state.

### 3.4 stt (Dragon -> Tab5)

```json
{
  "type": "stt",
  "text": "What is the weather like today?",
  "stt_ms": 342
}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"stt"` |
| `text` | string | Transcribed text from user speech. Empty string if no speech detected. |
| `stt_ms` | int | Optional. STT processing time in milliseconds. |

**When sent:** After Dragon finishes transcription of the audio buffer.

**Tab5 behavior on receive (Ask mode):**
1. Stores text in `s_stt_text` buffer.
2. Remains in `PROCESSING` state.
3. Updates UI to show what the user said.

**Note:** If `text` is empty, Dragon sends an `error` message with `"No speech detected"` and no further processing occurs.

### 3.5 llm (Dragon -> Tab5)

```json
{"type": "llm", "text": "It's"}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"llm"` |
| `text` | string | One token or small chunk of the LLM response. |

**When sent:** Streamed token-by-token as the LLM generates its response.

**Tab5 behavior on receive:**
1. Appends token to `s_llm_text` buffer.
2. Updates UI with streaming response text.
3. Remains in `PROCESSING` state.

### 3.6 llm_done (Dragon -> Tab5)

```json
{"type": "llm_done", "llm_ms": 1523}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"llm_done"` |
| `llm_ms` | int | Total LLM generation time in milliseconds. |

**When sent:** After the LLM has finished generating all tokens.

**Tab5 behavior on receive:** Logs the timing. No state transition (TTS follows).

### 3.7 tts_start (Dragon -> Tab5)

```json
{"type": "tts_start"}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"tts_start"` |

**When sent:** Before the first binary TTS audio chunk is sent. Sent once per utterance (even if the response contains multiple sentences).

**Tab5 behavior on receive:**
1. Enables the speaker (ES8388 DAC).
2. Resets the playback ring buffer.
3. Transitions to `SPEAKING` state.

### 3.8 Binary TTS Audio (Dragon -> Tab5)

Raw binary WebSocket frames containing TTS audio data.

| Parameter | Value |
|-----------|-------|
| Frame type | Binary |
| Encoding | PCM signed 16-bit little-endian |
| Sample rate | 16000 Hz (resampled from TTS engine native rate) |
| Channels | 1 (mono) |
| Chunk size | 4096 bytes |
| Pacing | ~80% real-time (first 4 chunks sent immediately, then paced) |

**When sent:** Between `tts_start` and `tts_end`. Dragon sends TTS audio in 4096-byte chunks, paced at approximately 80% of real-time playback speed to prevent Tab5's ring buffer from overflowing.

**Tab5 behavior on receive:**
1. Upsamples 16kHz to 48kHz using linear interpolation.
2. Writes to the playback ring buffer.
3. Playback drain task continuously pulls from the ring buffer and writes to I2S.

**Note:** If Tab5 receives binary audio while in `PROCESSING` state (before `tts_start`), it auto-transitions to `SPEAKING` state and begins playback.

### 3.9 tts_end (Dragon -> Tab5)

```json
{"type": "tts_end", "tts_ms": 892}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"tts_end"` |
| `tts_ms` | int | Optional. Total TTS synthesis time in milliseconds. |

**When sent:** After all binary TTS audio for the current response has been sent.

**Tab5 behavior on receive:**
1. Wakes the playback drain task one more time.
2. Spin-waits for the ring buffer to drain (max 10 seconds).
3. Disables the speaker.
4. Transitions to `READY` state.

---

## 4. Voice Dictation Flow

Dictation mode provides long-form speech-to-text without LLM processing or TTS response. Recording duration is unlimited. Tab5 performs client-side VAD with adaptive thresholds and sends segment markers on detected pauses.

### Sequence Diagram

```
Tab5                                    Dragon
  |                                       |
  |--- {"type":"start","mode":"dictate"}->|  Enter dictation mode
  |--- [binary PCM frames]            -->|  Continuous recording
  |--- [binary PCM frames]            -->|  ...
  |                                       |
  |  (Tab5 detects 500ms pause)           |
  |--- {"type":"segment"}             -->|  Segment boundary
  |<-- {"type":"stt_partial",          ---|  Partial transcript
  |      "text":"First sentence..."}   ---|
  |                                       |
  |--- [binary PCM frames]            -->|  Continues recording
  |  (Tab5 detects 500ms pause)           |
  |--- {"type":"segment"}             -->|  Another segment
  |<-- {"type":"stt_partial",          ---|  Another partial
  |      "text":"Second sentence..."}  ---|
  |                                       |
  |  (user stops, or 5s auto-stop)        |
  |--- {"type":"stop"}                -->|  Finalize dictation
  |<-- {"type":"stt_partial",...}       ---|  Final segment (if any)
  |<-- {"type":"stt","text":"Full..."} ---|  Combined transcript
  |                                       |
  |  (async, non-blocking)                |
  |<-- {"type":"dictation_summary",    ---|  Title + summary from LLM
  |      "title":"...","summary":"..."} --|
  |                                       |
```

### 4.1 start with dictate mode (Tab5 -> Dragon)

```json
{"type": "start", "mode": "dictate"}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"start"` |
| `mode` | string | `"dictate"` for dictation mode. Omit or `"ask"` for ask mode. |

**When sent:** When user initiates dictation (different UI action from push-to-talk).

**Dragon behavior:**
1. Clears audio buffer and segment buffer.
2. Clears accumulated dictation segments.
3. Sets pipeline to dictation mode (no server-side VAD, no LLM, no TTS).

**Tab5 behavior after sending:**
1. Allocates 64KB PSRAM buffer for accumulated transcript.
2. Spawns mic capture task with dictation-specific VAD.
3. Transitions to `LISTENING` state.

### 4.2 Client-Side VAD (Tab5 internal)

Tab5 performs adaptive VAD during dictation:

| Parameter | Value |
|-----------|-------|
| Silence threshold | Adaptive (calibrated from first 500ms of ambient noise) |
| Threshold range | 400 - 1500 RMS |
| Calibration formula | `max(400, min(1500, ambient_rms * 2.0))` |
| Calibration period | 25 frames (500ms) |
| Pause detection | 25 frames (500ms) of silence after speech |
| Auto-stop | 250 frames (5 seconds) of continuous silence after speech |

When a pause is detected (500ms silence after speech), Tab5 sends a `segment` message.

### 4.3 segment (Tab5 -> Dragon)

```json
{"type": "segment"}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"segment"` |

**When sent:** When Tab5 VAD detects a pause (500ms of silence after speech) during dictation.

**Dragon behavior:**
1. Takes the segment buffer (audio since last segment or start).
2. Runs STT transcription on just the segment.
3. Sends `stt_partial` with the segment text.
4. Clears the segment buffer.

### 4.4 stt_partial (Dragon -> Tab5)

```json
{
  "type": "stt_partial",
  "text": "This is the transcribed segment.",
  "stt_ms": 156
}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"stt_partial"` |
| `text` | string | Transcribed text from this segment. |
| `stt_ms` | int | Optional. STT processing time for this segment. |

**When sent:** After processing each dictation segment.

**Tab5 behavior on receive (dictation mode only):**
1. Appends text to the accumulated dictation transcript (space-separated).
2. Updates UI with the running transcript (via state callback with accumulated text).
3. Remains in `LISTENING` state.

### 4.5 stop in dictation mode (Tab5 -> Dragon)

Same `stop` message as Ask mode:

```json
{"type": "stop"}
```

**When sent:** User manually stops dictation, or Tab5 auto-stops after 5 seconds of continuous silence.

**Dragon behavior in dictation mode:**
1. Transcribes any remaining audio in the segment buffer (sends `stt_partial`).
2. Joins all segment transcripts into the full text.
3. Sends `stt` with the combined full transcript.
4. Resets dictation state.
5. Asynchronously triggers LLM post-processing for title + summary (see [Dictation Post-Processing](#11-dictation-post-processing)).

**Tab5 behavior:**
- On receiving `stt`: stores final text, transitions to `READY` with detail `"dictation_done"`.
- No `llm`, `tts_start`, or `tts_end` messages are sent for dictation.

---

## 5. Text Input Flow

Text input skips STT entirely and goes straight to the conversation engine. Dragon responds with streaming LLM tokens and TTS audio.

### Sequence Diagram

```
Tab5                                    Dragon
  |                                       |
  |--- {"type":"text",                 -->|   Text input
  |     "content":"What time is it?"}     |
  |                                       |
  |<-- {"type":"llm","text":"It"}      ---|   Streaming tokens
  |<-- {"type":"llm","text":"'s"}      ---|
  |<-- {"type":"llm","text":" 3pm."} ---|
  |<-- {"type":"llm_done","llm_ms":N}  ---|   LLM complete
  |<-- {"type":"tts_start"}            ---|   TTS begins
  |<-- [binary TTS audio]             ---|   Audio chunks
  |<-- {"type":"tts_end","tts_ms":N}   ---|   TTS complete
  |                                       |
```

### 5.1 text (Tab5 -> Dragon)

```json
{
  "type": "text",
  "content": "What time is it?"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"text"` |
| `content` | string | The text message to process. Must not be empty. |

**When sent:** When user types text via the on-screen keyboard or sends text via API.

**Tab5 behavior after sending:**
1. Stores content as `s_stt_text` (for UI display as "what the user said").
2. Clears `s_llm_text`.
3. Transitions to `PROCESSING` state.

**Dragon behavior:**
1. Validates session is registered.
2. Streams LLM response tokens via `llm` messages.
3. Sends `llm_done`.
4. If `response_mode` is `"always_speak"` (default for voice devices): synthesizes TTS and sends `tts_start`, binary audio, `tts_end`.
5. If `response_mode` is `"match_input"`: text input gets text-only response (no TTS).

**Note:** The `stt` message is NOT sent for text input -- there is no speech to transcribe. The flow starts directly with `llm` tokens.

---

## 6. Control Messages

### 6.1 cancel (Tab5 -> Dragon)

```json
{"type": "cancel"}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"cancel"` |

**When sent:**
- User taps cancel during processing or playback.
- Tab5 auto-cancels on response timeout (35 seconds with no incoming data).

**Dragon behavior:**
1. Sets cancelled flag on the pipeline.
2. Cancels any in-progress asyncio task (STT, LLM, TTS).
3. Clears audio and segment buffers.

**Tab5 behavior after sending:**
1. Stops mic capture if running.
2. Resets playback ring buffer.
3. Disables speaker.
4. Transitions to `READY` (if WebSocket connected) or `IDLE` (if disconnected).

### 6.2 clear (Tab5 -> Dragon)

```json
{"type": "clear"}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"clear"` |

**When sent:** User explicitly requests to start a fresh conversation (clear history button).

**Dragon behavior:**
1. Clears in-memory LLM conversation history on the pipeline.
2. Ends the current session in the database.
3. Creates a new session for the same device.
4. Sends a new `session_start` message with the new session ID.

**Expected response:** `session_start` with `resumed: false` and `message_count: 0`.

**Example response:**

```json
{
  "type": "session_start",
  "session_id": "new-session-uuid",
  "device_id": "aabbccddeeff",
  "resumed": false,
  "message_count": 0
}
```

### 6.3 ping (Tab5 -> Dragon)

```json
{"type": "ping"}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"ping"` |

**When sent:** Every 15 seconds during `PROCESSING` or `SPEAKING` states to prevent TCP idle timeout.

**Why application-level ping:** ESP-IDF's `esp_transport_ws` fragments WebSocket control frames, which aiohttp rejects as invalid. Therefore, keepalive uses application-level JSON text frames instead of WebSocket ping/pong frames.

**IMPORTANT:** Sending a keepalive ping does NOT reset the response timeout timer on Tab5. Only real incoming data (STT, LLM, TTS) resets the timer. This ensures the response timeout can still fire even if keepalive pings succeed.

### 6.4 pong (Dragon -> Tab5)

```json
{"type": "pong"}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"pong"` |

**When sent:** Immediately in response to a `ping` message.

**Tab5 behavior on receive:** No action (logged at debug level only).

### 6.5 config_update (Tab5 -> Dragon)

Tab5 can request cloud mode toggle:

```json
{
  "type": "config_update",
  "cloud_mode": true
}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"config_update"` |
| `cloud_mode` | bool | `true` to switch STT + TTS to OpenRouter cloud backends. `false` for local (Moonshine + Piper). |

**When sent:** User toggles cloud mode in Settings UI.

**Dragon behavior:**
1. Switches STT backend: `"moonshine"` (local) or `"openrouter"` (cloud).
2. Switches TTS backend: `"piper"` (local) or `"openrouter"` (cloud).
3. Hot-swaps backends on the active pipeline.
4. Sends confirmation `config_update` back to Tab5.

### 6.6 ready_ack (Tab5 -> Dragon)

Tab5 notifies Dragon that the playback ring has fully drained and the
voice state has returned to `READY` after a TTS playback.  Pure
observability — Dragon logs the event but takes no other action.

```json
{
  "type": "ready_ack",
  "mode": 3
}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"ready_ack"` |
| `mode` | int | Active voice_mode at end-of-turn (0=local, 1=hybrid, 2=cloud, 3=tinkerclaw, 4=onboard, 5=solo). |

**When sent:** After Tab5's playback drain task transitions the orb back to `VOICE_STATE_READY` (TT #564 / PR #565).

**Dragon behavior:** Logs the event.  No state change.  Future use: cross-stack observability (e.g. Dragon could verify Tab5 actually finished playback before sending the next turn's tts_start in pipelined scenarios).

### 6.7 config_update (Dragon -> Tab5)

Dragon confirms the configuration change (or can push config changes unprompted):

```json
{
  "type": "config_update",
  "config": {
    "stt": "openrouter",
    "tts": "openrouter",
    "llm": "openrouter",
    "cloud_mode": true
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"config_update"` |
| `config` | object | Updated configuration values. |
| `config.cloud_mode` | bool | Current cloud mode state. |
| `config.stt` | string | Active STT backend name. |
| `config.tts` | string | Active TTS backend name. |
| `config.llm` | string | Active LLM backend name. |

**When sent:** In response to a Tab5 `config_update` request, or proactively when Dragon changes config.

**Tab5 behavior on receive:**
1. Reads `cloud_mode` from `config` object.
2. Persists to NVS settings.

---

## 7. Session Management

### Session Lifecycle

```
                  +-- register (new) --> CREATE --> ACTIVE --+
                  |                                          |
 Tab5 connects -->+                                          +--> WS disconnect --> PAUSED
                  |                                          |
                  +-- register (resume) --> RESUME --> ACTIVE-+
                                                             |
                                             timeout (30m) --+--> ENDED
                                                             |
                                             clear command --+--> ENDED (new session created)
```

| State | Description |
|-------|-------------|
| ACTIVE | WebSocket connected, device registered, conversation in progress. |
| PAUSED | WebSocket disconnected. Session is preserved. Can be resumed. |
| ENDED | Session is finalized. Cannot be resumed. |

### Session Resume Flow

1. Tab5 stores `session_id` from `session_start` in NVS.
2. On reconnect, Tab5 sends stored `session_id` in the `register` message.
3. Dragon checks if the session exists and is in `paused` status.
4. If valid: resumes the session. `session_start` has `resumed: true` and `message_count > 0`.
5. If invalid or expired: creates a new session. `session_start` has `resumed: false`.

### Session ID Storage

- Tab5 stores `session_id` in NVS (non-volatile storage on ESP32).
- Survives power cycles and reboots.
- Cleared only on explicit factory reset.
- If `session_id` is empty string or not set, Tab5 sends `null` in the register message.

### Session vs. Connection

Sessions and WebSocket connections are independent:

- A session can span multiple WebSocket connections (disconnect + reconnect = same session).
- A WebSocket disconnect pauses the session, it does NOT end it.
- All messages are preserved in the database across reconnects.
- The `clear` command ends the current session and creates a new one.

---

## 8. Error Handling

### 8.1 error (Dragon -> Tab5)

```json
{
  "type": "error",
  "code": "stt_failed",
  "message": "Transcription failed: model not loaded"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"error"` |
| `code` | string | Optional. Machine-readable error code. |
| `message` | string | Human-readable error description. |

**Error codes:**

| Code | Description |
|------|-------------|
| `session_invalid` | Device not registered, or session not found. |
| `internal` | Pipeline initialization or internal server error. |
| `llm_failed` | LLM text processing failed. |
| `stt_failed` | STT transcription failed. |
| `tts_failed` | TTS synthesis failed. |

**Tab5 behavior on receive:**
1. Logs the error message.
2. Stops any playback in progress (resets ring buffer, disables speaker).
3. Transitions to `READY` if WebSocket is still connected (transient error).
4. Transitions to `IDLE` if WebSocket is disconnected.

### Response Timeout (Tab5-side)

| Parameter | Value |
|-----------|-------|
| Timeout duration | 35 seconds |
| Starts after | `stop` message sent (activity timestamp reset) |
| Applies during | `PROCESSING` and `SPEAKING` states |
| Action on timeout | Send `cancel`, reset playback, transition to `READY` |

The timeout is measured from the last incoming data (not keepalive pings). If Dragon hasn't sent any STT, LLM, or TTS data within 35 seconds, Tab5 auto-cancels. The 35-second value exceeds Dragon's own 30-second TTS synthesis timeout to avoid premature cancellation.

### WebSocket-Level Error Handling

| Event | Tab5 behavior |
|-------|---------------|
| WebSocket PING frame from server | Respond with PONG (handled by transport layer) |
| WebSocket CLOSE frame | Mark disconnected, clean up transport, transition to IDLE |
| Poll error | Mark disconnected, break receive loop |
| Read error | Mark disconnected, break receive loop |
| Send error | Mark disconnected, log warning |

---

## 9. Audio Format

### Tab5 -> Dragon (Mic Audio)

| Parameter | Value |
|-----------|-------|
| Encoding | PCM signed 16-bit little-endian (int16) |
| Sample rate | 16000 Hz |
| Channels | 1 (mono) |
| Chunk duration | 20ms |
| Chunk size | 640 bytes (320 samples) |
| Bit rate | 256 kbps |
| Source | ES7210 quad-mic ADC, TDM slot 0 (MIC-L) |

**Tab5 internal pipeline:**
1. ES7210 captures at 48kHz, 4 TDM channels.
2. Extract slot 0 (MIC-L) from interleaved TDM buffer.
3. Downsample 3:1 (48kHz to 16kHz) using box filter (average of 3 samples).
4. Send 20ms chunks over WebSocket.

### Dragon -> Tab5 (TTS Audio)

| Parameter | Value |
|-----------|-------|
| Encoding | PCM signed 16-bit little-endian (int16) |
| Sample rate | 16000 Hz |
| Channels | 1 (mono) |
| Chunk size | 4096 bytes (2048 samples, ~128ms) |
| Pacing | ~80% real-time after initial burst |

**Dragon internal pipeline:**
1. TTS engine synthesizes at native rate (e.g. Piper at 22050 Hz).
2. Linear interpolation resample to 16000 Hz.
3. Send in 4096-byte chunks over WebSocket.
4. First 4 chunks sent immediately (pre-buffer for Tab5 ring buffer).
5. Subsequent chunks paced at `(4096 / 2) / 16000 * 0.8 = ~0.1s` per chunk.

**Tab5 internal playback pipeline:**
1. Receive 16kHz chunks from WebSocket.
2. Upsample 1:3 (16kHz to 48kHz) using linear interpolation.
3. Write to playback ring buffer (128KB PSRAM, ~1.4s at 48kHz).
4. Playback drain task pulls from ring buffer and writes to I2S (ES8388 DAC).

### Playback Ring Buffer

| Parameter | Value |
|-----------|-------|
| Size | 131072 bytes (128 KB) |
| Location | PSRAM (external) |
| Capacity | ~1.4 seconds at 48kHz mono 16-bit |
| Drain task priority | 6 (higher than WS receive at 4) |

---

## 10. State Machine

### Tab5 Voice States

```
                            voice_connect()
    IDLE -----------------------------------------> CONNECTING
     ^                                                  |
     |                                    session_start received
     |                                                  |
     |                                                  v
     |  disconnect/error                             READY <--+
     +----------------------------------+             |  |     |
                                        |    start/   |  |     |
                                        |   dictate   |  | tts_end /
                                        |             v  | dictation_done /
                                        |         LISTENING  | cancel /
                                        |             |      | timeout
                                        |        stop |      |
                                        |             v      |
                                        +-------- PROCESSING-+
                                        |             |
                                        |   tts_start |
                                        |      or     |
                                        |   binary    |
                                        |   audio     |
                                        |             v
                                        +--------- SPEAKING--+
                                                      |      |
                                                tts_end      |
                                                      |      |
                                                      +------+
```

### State Descriptions

| State | Description | Valid transitions |
|-------|-------------|-------------------|
| `IDLE` | Not connected. Waiting for connection request. | -> `CONNECTING` (on `voice_connect`) |
| `CONNECTING` | WebSocket connecting. Waiting for `session_start`. | -> `READY` (on `session_start`), -> `IDLE` (on failure) |
| `READY` | Connected and registered. Waiting for user action. | -> `LISTENING` (on start/dictate), -> `PROCESSING` (on text send), -> `IDLE` (on disconnect) |
| `LISTENING` | Mic active, streaming audio to Dragon. | -> `PROCESSING` (on stop), -> `READY` (on cancel), -> `IDLE` (on disconnect) |
| `PROCESSING` | Waiting for Dragon STT/LLM response. | -> `SPEAKING` (on tts_start or binary audio), -> `READY` (on dictation_done, cancel, timeout, error), -> `IDLE` (on disconnect) |
| `SPEAKING` | Playing TTS audio from Dragon. | -> `READY` (on tts_end, cancel), -> `IDLE` (on disconnect) |

### State Transition Triggers

| Trigger | From | To | Message/Event |
|---------|------|----|---------------|
| `voice_connect()` called | IDLE | CONNECTING | -- |
| `session_start` received | CONNECTING | READY | `{"type":"session_start"}` |
| Connect failure | CONNECTING | IDLE | -- |
| `voice_start_listening()` | READY | LISTENING | Sends `{"type":"start"}` |
| `voice_start_dictation()` | READY | LISTENING | Sends `{"type":"start","mode":"dictate"}` |
| `voice_stop_listening()` | LISTENING | PROCESSING | Sends `{"type":"stop"}` |
| Dictation auto-stop (5s silence) | LISTENING | PROCESSING | Sends `{"type":"stop"}` |
| 30s recording limit (ask) | LISTENING | PROCESSING | Sends `{"type":"stop"}` |
| `stt` received (dictation) | PROCESSING | READY | `{"type":"stt"}` |
| `tts_start` received | PROCESSING | SPEAKING | `{"type":"tts_start"}` |
| Binary audio received | PROCESSING | SPEAKING | Binary frame (auto-transition) |
| `tts_end` received | SPEAKING | READY | `{"type":"tts_end"}` |
| `voice_cancel()` called | any | READY/IDLE | Sends `{"type":"cancel"}` |
| Response timeout (35s) | PROCESSING/SPEAKING | READY | Sends `{"type":"cancel"}` |
| `error` received | any | READY/IDLE | `{"type":"error"}` |
| WebSocket disconnect | any | IDLE | -- |
| `voice_send_text()` | READY | PROCESSING | Sends `{"type":"text"}` |

---

## 11. Dictation Post-Processing

After a dictation session completes (full transcript sent via `stt`), Dragon asynchronously generates a title and summary using the LLM. This runs in the background and does not block the main pipeline.

### dictation_summary (Dragon -> Tab5)

```json
{
  "type": "dictation_summary",
  "title": "Meeting notes about Q2 goals",
  "summary": "Discussion covered quarterly targets, team allocation, and the upcoming product launch timeline."
}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"dictation_summary"` |
| `title` | string | Short title for the dictation (max ~8 words). |
| `summary` | string | 1-2 sentence summary of the dictation content. |

**When sent:** Asynchronously after dictation completes, if the transcript is longer than 20 characters. May arrive seconds after the `stt` message since LLM inference is required.

**Tab5 behavior on receive:**
1. Stores title in `s_dictation_title` (128 bytes max).
2. Stores summary in `s_dictation_summary` (512 bytes max).
3. Fires state callback with detail `"dictation_summary"` (allows UI to update with title/summary).

**Dragon generation prompt:**
Dragon sends the transcript (truncated to 2000 chars) to the LLM with a summarization prompt requesting a title (max 8 words) and a 1-2 sentence summary.

---

## 12. Tool Execution Events

When the LLM decides to call a tool during response generation, Dragon sends real-time events to the connected client so the UI can show tool activity. These messages are interleaved with `llm` token messages during the response stream.

### 12.1 tool_call (Dragon -> Tab5)

```json
{
  "type": "tool_call",
  "tool": "web_search",
  "args": {"query": "weather in Dublin today"}
}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"tool_call"` |
| `tool` | string | Name of the tool being invoked (e.g. `web_search`, `remember`, `recall`, `datetime`). |
| `args` | object | Arguments passed to the tool, as parsed from the LLM output. |

**When sent:** After the LLM outputs tool-call markers and Dragon parses them, before tool execution begins.

**Tab5 behavior on receive:** Display a tool activity indicator (e.g. "Searching...") in the UI. Remain in `PROCESSING` state.

### 12.2 tool_result (Dragon -> Tab5)

```json
{
  "type": "tool_result",
  "tool": "web_search",
  "result": {"snippets": ["Dublin: 14°C, partly cloudy..."]},
  "execution_ms": 234
}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"tool_result"` |
| `tool` | string | Name of the tool that was executed. |
| `result` | object | The tool's return value. Structure varies by tool. |
| `execution_ms` | int | Wall-clock time for tool execution in milliseconds. |

**When sent:** After tool execution completes, before the LLM continues generating with the tool result injected into context.

**Tab5 behavior on receive:** Update the tool activity indicator with the result. Remain in `PROCESSING` state. The LLM will continue generating `llm` tokens after this.

**Note:** Up to 3 tool calls may occur per turn (Dragon enforces a max to prevent infinite loops). Each tool call produces a `tool_call` + `tool_result` pair.

### 12.3 Complete Tool-Calling Example Flow

Full message sequence for a tool-calling conversation (text input asking for the time):

```
Tab5 → Dragon: {"type":"text","content":"What time is it?"}
Dragon internal: LLM generates <tool>datetime</tool><args>{"query":"time"}</args>
Dragon → Tab5: {"type":"tool_call","tool":"datetime","args":{"query":"time"}}
Dragon internal: Tool executes, returns result
Dragon → Tab5: {"type":"tool_result","tool":"datetime","result":{"datetime":"2026-04-07T14:30:00","timezone":"UTC"},"execution_ms":1}
Dragon internal: LLM re-queries with tool result injected into context
Dragon → Tab5: {"type":"llm","text":"It"}
Dragon → Tab5: {"type":"llm","text":"'s"}
Dragon → Tab5: {"type":"llm","text":" 2"}
Dragon → Tab5: {"type":"llm","text":":30"}
Dragon → Tab5: {"type":"llm","text":" PM"}
Dragon → Tab5: {"type":"llm","text":"."}
Dragon → Tab5: {"type":"llm_done","llm_ms":5000}
Dragon → Tab5: {"type":"tts_start"}
Dragon → Tab5: [binary TTS audio chunks]
Dragon → Tab5: {"type":"tts_end","tts_ms":400}
```

**Key points:**
- `tool_call` and `tool_result` are interleaved between `text` input and the final `llm` token stream
- The LLM generates tokens TWICE: once to produce the tool markers, then again with the tool result to produce the final answer
- Tab5 stays in `PROCESSING` state throughout the tool execution phase
- Multiple tool calls can chain (up to 3 per turn) — each produces its own `tool_call` + `tool_result` pair before the final LLM response

---

## 13. OTA Protocol

Dragon serves firmware updates for Tab5 via HTTP endpoints on port 3502.

### 13.1 Check for Updates

```
Tab5 → Dragon: GET /api/ota/check?current=0.6.0
Dragon → Tab5: {"update":true,"version":"0.6.1","url":"http://192.168.1.89:3502/api/ota/firmware.bin","sha256":"abc123..."}
```

If no update is available:
```
Dragon → Tab5: {"update":false,"version":"0.6.0"}
```

### 13.2 Download and Apply

```
Tab5: Downloads firmware via esp_https_ota from the URL in the check response
Tab5: Writes firmware to inactive OTA partition (ota_0 or ota_1)
Tab5: Verifies SHA256 hash matches
Tab5: Reboots into new firmware
Tab5: New firmware boots in PENDING_VERIFY state
Tab5: If stable, calls tab5_ota_mark_valid() → firmware committed
Tab5: If crash before mark_valid, bootloader auto-reverts to previous partition
```

### 13.3 Server-Side Files

Dragon serves from `/home/radxa/ota/`:
- `version.json` — `{"version":"0.6.1","sha256":"abc123..."}` — compared against `?current=` param
- `tinkertab.bin` — firmware binary, streamed in 8KB chunks

---

## 14. config_update Backward Compatibility

Both old and new config_update formats are supported by Dragon:

### Old Format (boolean)
```json
{"type": "config_update", "cloud_mode": true}
```
Maps to: `voice_mode=2` (Full Cloud) if `true`, `voice_mode=0` (Local) if `false`.

### New Format (three-tier)
```json
{"type": "config_update", "voice_mode": 0, "llm_model": "anthropic/claude-3-haiku"}
```
Direct integer mode (0=Local, 1=Hybrid, 2=Full Cloud) with explicit model selection.

**Note:** The old boolean format cannot express Hybrid mode (voice_mode=1). New clients should always use the integer format. Dragon accepts both for backward compatibility with older Tab5 firmware versions.

---

## 15. Rich Media Messages

Dragon can render rich content from LLM responses (code blocks, tables, image URLs) as JPEG images and serve them to Tab5 for inline display. Media detection runs after `llm_done` in both ConvEngine and TinkerClaw code paths.

### 15.1 media (Dragon -> Tab5)

Sent when a code block, table, or image URL in the LLM response has been rendered as an image.

```json
{
  "type": "media",
  "media_type": "image",
  "url": "/api/media/a1b2c3d4",
  "width": 660,
  "height": 420,
  "alt": "Code: python"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"media"` |
| `media_type` | string | `"image"` (rendered code/table/downloaded image). |
| `url` | string | Relative path to fetch the media file (GET /api/media/{id}). |
| `width` | int | Image width in pixels (max 660). |
| `height` | int | Image height in pixels (variable). |
| `alt` | string | Accessibility description (e.g. `"Code: python"`, `"Table"`, `"Image"`). |

**When sent:** After `llm_done`, once the MediaPipeline has rendered the content. Up to 3 media items per response.

**Tab5 behavior on receive:** Fetch the image from the URL and display it inline in the chat bubble below the AI text.

### 15.2 card (Dragon -> Tab5)

Rich card with optional image, title, subtitle, and description.

```json
{
  "type": "card",
  "title": "Weather in Dublin",
  "subtitle": "14°C, Partly Cloudy",
  "image_url": "/api/media/e5f6g7h8",
  "description": "Wind: 12 km/h NW. Humidity: 78%."
}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"card"` |
| `title` | string | Card title. |
| `subtitle` | string | Optional. Secondary text. |
| `image_url` | string | Optional. Relative path to card image. |
| `description` | string | Optional. Body text. |

**When sent:** When the LLM response contains structured data suitable for card display.

**Tab5 behavior on receive:** Render a styled card in the chat UI.

### 15.3 audio_clip (Dragon -> Tab5)

Audio player card for non-TTS audio content.

```json
{
  "type": "audio_clip",
  "url": "/api/media/i9j0k1l2",
  "duration_s": 12.5,
  "label": "Voice memo"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"audio_clip"` |
| `url` | string | Relative path to audio file (WAV). |
| `duration_s` | float | Duration in seconds. |
| `label` | string | Display label for the audio player. |

**When sent:** When the response includes an audio file that should be played on demand (not auto-played like TTS).

**Tab5 behavior on receive:** Show an audio player widget. User taps to play.

### 15.4 text_update (Dragon -> Tab5)

Replaces the text in the last AI chat bubble. Used after code blocks have been rendered as images — the raw markdown code is stripped from the text.

```json
{
  "type": "text_update",
  "text": "Here is the solution:\n\n(see image above)\n\nLet me know if you need changes."
}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"text_update"` |
| `text` | string | Replacement text for the last AI bubble (code blocks stripped). May be `""` if the entire response was a code block that was rendered as media. |

**When sent (audit D6, 2026-04-20):** BEFORE any `media` messages for the same turn, whenever `strip_rendered_content()` has modified the response. Sending order is load-bearing — Tab5 targets the tail of the chat store when updating, and the `media` event appends a new bubble to that tail. If `text_update` arrived after the media event, the clear would hit the wrong bubble. Always emit `text_update` first, then the media events.

**Tab5 behavior on receive:**
- Non-empty text: replace the text content of the most recent AI chat bubble.
- **Empty text (`""`)**: remove the most recent AI chat bubble entirely via `chat_store_pop_last()`. This is the "whole response was a code block that moved into media" case — the raw markdown bubble is no longer needed because the rendered JPEG will render alone.

**Regression test:** `TinkerBox/tests/audit/test_d5_d6_ws.py` exercises this contract end-to-end against a live Dragon, asserting `text_update.index < media.index` in the event stream.

### 15.5 user_media (Tab5 -> Dragon)

User sends a camera photo for multimodal LLM analysis.

```json
{
  "type": "user_media",
  "media_id": "m3n4o5p6",
  "media_type": "image",
  "text": "What is this plant?"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"user_media"` |
| `media_id` | string | ID from a prior `POST /api/media/upload` response. |
| `media_type` | string | `"image"` (camera photo). |
| `text` | string | Optional. User's question about the image. |

**When sent:** After Tab5 captures a photo and uploads it via `POST /api/media/upload`, it sends this message with the returned `media_id` and an optional text prompt.

**Dragon behavior on receive:**
1. Loads the image from MediaStore using `media_id`.
2. Passes the image + text to the LLM for multimodal analysis.
3. Streams the response as normal (`llm` tokens → `llm_done` → optional TTS).

### 15.6 REST Endpoints for Media

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/media/{id}` | Serve rendered media file (JPEG/PNG/WAV). Response includes `Cache-Control: max-age=3600`. Returns 404 if media_id not found or expired. |
| POST | `/api/media/upload` | Accept BMP/JPEG from Tab5 camera. Converts to JPEG, resizes to max 660px width via Pillow. Returns `{"media_id":"..."}`. |

### 15.7 MediaPipeline Flow

```
LLM response text
       |
       v
process_response()
       |
       +-- Regex scan for:
       |     ```lang...```  (code blocks)
       |     |col|col|      (markdown tables)
       |     https://...jpg (image URLs)
       |
       +-- Code blocks → Pygments ImageFormatter (dark theme) → JPEG
       +-- Tables → Pillow styled grid (orange headers, dark bg) → JPEG
       +-- Image URLs → aiohttp download → resize 660px max → JPEG q80
       |
       +-- All stored in MediaStore (/home/radxa/media/)
       +-- Max 3 media items per response
       |
       v
Send media/card/audio_clip messages to Tab5
       |
       v
strip_rendered_content() → text_update message
```

---

## 16. Message Reference

### All Messages: Tab5 -> Dragon

| Type | Format | When | Expected Response |
|------|--------|------|-------------------|
| `register` | `{"type":"register","device_id":"...","hardware_id":"...","name":"...","firmware_ver":"...","platform":"...","session_id":null,"capabilities":{...}}` | First frame after WS connect | `session_start` |
| `start` | `{"type":"start"}` | Push-to-talk begin | None (start sending binary) |
| `start` (dictate) | `{"type":"start","mode":"dictate"}` | Dictation begin | None (start sending binary) |
| Binary PCM | 640 bytes (20ms at 16kHz mono int16) | During LISTENING | None |
| `segment` | `{"type":"segment"}` | Dictation pause detected | `stt_partial` |
| `stop` | `{"type":"stop"}` | Push-to-talk end | `stt` -> `llm` -> `tts_start` -> binary -> `tts_end` (ask) or `stt` (dictation) |
| `text` | `{"type":"text","content":"..."}` | Text input | `llm` -> `llm_done` -> `tts_start` -> binary -> `tts_end` |
| `cancel` | `{"type":"cancel"}` | User cancels or timeout | None |
| `clear` | `{"type":"clear"}` | Clear history | `session_start` (new session) |
| `ping` | `{"type":"ping"}` | Every 15s during PROCESSING/SPEAKING | `pong` |
| `config_update` | `{"type":"config_update","cloud_mode":true}` | Cloud mode toggle | `config_update` (confirmation) |
| `config_ack` | `{"type":"config_ack","applied":{...}}` | Tab5 confirming receipt of Dragon's `config_update` response | None (Dragon logs at debug level) |
| `user_media` | `{"type":"user_media","media_id":"...","media_type":"image","text":"..."}` | Camera photo for multimodal LLM | `llm` -> `llm_done` -> optional TTS |
| `widget_action` | `{"type":"widget_action","session_id":"...","card_id":"...","event":"..."}` | User tapped a prompt choice / card button (Widget Platform §15) | Skill-specific (delegated to `surface_mgr.handle_action`) |
| `channel_reply` | `{"type":"channel_reply","channel":"telegram","thread_id":"...","text":"..."}` | User reply to a `channel_message` (W7-E.4 / W7-E.4b — voice-dictated or typed) | `channel_reply_ack` |

### All Messages: Dragon -> Tab5

| Type | Format | When | Tab5 Action |
|------|--------|------|-------------|
| `session_start` | `{"type":"session_start","session_id":"...","device_id":"...","resumed":false,"message_count":0,"config":{...}}` | After register | Store session_id, transition to READY |
| `stt` | `{"type":"stt","text":"..."}` | After STT completes | Store transcript, stay PROCESSING (ask) or go READY (dictation) |
| `stt_partial` | `{"type":"stt_partial","text":"..."}` | After dictation segment | Append to dictation text, stay LISTENING |
| `llm` | `{"type":"llm","text":"..."}` | Streaming LLM tokens | Append to LLM buffer, update UI |
| `llm_done` | `{"type":"llm_done","llm_ms":1234}` | LLM generation complete | Log timing |
| `tts_start` | `{"type":"tts_start"}` | Before TTS audio | Enable speaker, transition to SPEAKING |
| Binary TTS | 4096-byte PCM chunks at 16kHz mono int16 | During TTS playback | Upsample 16k->48k, write to ring buffer |
| `tts_end` | `{"type":"tts_end","tts_ms":892}` | After all TTS audio sent | Drain buffer, disable speaker, transition to READY |
| `error` | `{"type":"error","code":"...","message":"..."}` | On any error | Log, stop playback, transition READY/IDLE |
| `pong` | `{"type":"pong"}` | In response to ping | None |
| `config_update` | `{"type":"config_update","config":{"cloud_mode":true,"stt":"...","tts":"...","llm":"..."}}` | After config change | Persist cloud_mode to NVS |
| `tool_call` | `{"type":"tool_call","tool":"web_search","args":{"query":"..."}}` | During LLM tool execution | Display tool activity indicator |
| `channel_message` | `{"type":"channel_message","channel":"tg","message_id":"...","thread_id":"...","sender":{"display_name":"...","starred":bool},"text":"...","preview":"...","priority":"low|normal|high","needs_reply":bool}` | Third-party platform message routed through OpenClaw gateway → Dragon → Tab5 (W7-E + W7-F).  Full schema: §20.1 | Route to toast or now-card per W7-E.2 rules; fire `UI_CUE_INCOMING_*` audio cue unless quiet-hours active |
| `channel_reply_ack` | `{"type":"channel_reply_ack","channel":"telegram","thread_id":"...","ok":bool,"platform_message_id":"...","error":"..."}` | Confirms outcome of a `channel_reply` Tab5 sent (W7-F stub OR real GatewayConnector ACK).  Full schema: §20.3 | On `ok=true` toast "Replied via {channel}"; on `ok=false` show error toast |
| `tool_result` | `{"type":"tool_result","tool":"web_search","result":{...},"execution_ms":234}` | After tool execution completes | Display tool result, update UI |
| `dictation_summary` | `{"type":"dictation_summary","title":"...","summary":"..."}` | After dictation post-processing | Store title/summary, update UI |
| `media` | `{"type":"media","media_type":"image","url":"/api/media/{id}","width":660,"height":N,"alt":"Code: python"}` | After llm_done (rendered code/table/image) | Fetch image, display inline in chat |
| `card` | `{"type":"card","title":"...","subtitle":"...","image_url":"...","description":"..."}` | Rich content display | Render styled card in chat UI |
| `audio_clip` | `{"type":"audio_clip","url":"...","duration_s":N,"label":"..."}` | Non-TTS audio content | Show audio player widget |
| `text_update` | `{"type":"text_update","text":"..."}` | After media rendered, code stripped from text | Replace last AI bubble text |

---

## 17. Widget Platform Messages (v1, April 2026)

**Added with Widget Platform v1.** Extends the rich-media pipeline (§15) with
a typed skill-surface vocabulary. Skills on Dragon emit typed widget state;
devices render it opinionatedly.

See TinkerTab `docs/WIDGETS.md` for the full design spec, rendering contract,
and authoring guide. This section is the on-the-wire protocol only.

### 17.1 Overview

Six widget types. Messages are backward-compatible additions — existing
clients that don't recognize a `widget_*` type simply ignore it.

| Direction | Type | Purpose |
|---|---|---|
| Dragon → Tab5 | `widget_live` | Create or replace the one-at-a-time live widget |
| Dragon → Tab5 | `widget_live_update` | Partial update to current live widget |
| Dragon → Tab5 | `widget_live_dismiss` | Remove live widget (home returns to idle) |
| Dragon → Tab5 | `widget_card` | Push a card into the activity stream / chat |
| Dragon → Tab5 | `widget_list` | Show a pick-one-of-N modal |
| Dragon → Tab5 | `widget_chart` | Show a data shape (spark / bar / gauge) |
| Dragon → Tab5 | `widget_media` | Image + caption (alias of §15 `media` with skill_id) |
| Dragon → Tab5 | `widget_prompt` | Ask one question with typed input |
| Dragon → Tab5 | `widget_dismiss` | Dismiss any non-live widget by card_id |
| Tab5 → Dragon | `widget_action` | User tapped action or answered prompt |
| Tab5 → Dragon | `register.capabilities.widgets` | Widget renderer limits — nested inline in §2.1 `register`, **not** a standalone frame (W14-H19). |

### 17.2 Live widget — `widget_live`

```json
{
  "type": "widget_live",
  "skill_id": "timesense.pomodoro",
  "card_id": "ts_25",
  "title": "Deep work",
  "body": "25:00 remaining",
  "icon": "briefcase",
  "tone": "calm",
  "progress": 0.0,
  "action": {"label": "PAUSE", "event": "ts.pause"},
  "priority": 80,
  "expires_ms": 1500000
}
```

**Slots:**
- `skill_id` — required; dotted namespace (owner.skill)
- `card_id` — required; stable across updates
- `title` — required; ≤32 chars; renders as serif display on Tab5
- `body` — required; ≤80 chars; wraps max 2 lines
- `icon` — optional; one of 16 v1 built-in ids (see TinkerTab `docs/WIDGETS.md` §6)
- `tone` — required; one of `calm|active|approaching|urgent|done`
- `progress` — optional; 0.0–1.0; drives orb ring + size
- `action` — optional; single tappable action
- `priority` — optional (default 50); 0–100; brain sorts by priority × age
- `expires_ms` — optional; TTL from push; auto-dismiss after

**Tone → Tab5 rendering:** see TinkerTab `docs/WIDGETS.md` §3.1 mapping table.

### 17.3 Live update — `widget_live_update`

Partial. Only send slots that changed.

```json
{
  "type": "widget_live_update",
  "card_id": "ts_25",
  "body": "23:58 remaining",
  "progress": 0.08,
  "tone": "active"
}
```

Tab5 merges into its local widget_store entry. If `card_id` is unknown
(maybe the dismiss race landed first), Tab5 drops silently.

### 17.4 Live dismiss — `widget_live_dismiss`

```json
{"type": "widget_live_dismiss", "card_id": "ts_25"}
```

Tab5 marks the widget inactive. If this was the highest-priority active live
widget, home returns to idle or promotes the next-highest.

### 17.5 Card — `widget_card`

Moment-in-conversation notification or tappable completion. Lands in chat
stream (via existing `chat_msg_view.c` card renderer, extended with optional
action).

```json
{
  "type": "widget_card",
  "skill_id": "timesense.pomodoro",
  "card_id": "ts_done_25",
  "title": "Done",
  "body": "25 min · well run",
  "tone": "success",
  "icon": "check",
  "action": {"label": "START ANOTHER", "event": "ts.start_next"}
}
```

**Slots:**
- `skill_id`, `card_id` — as live
- `title` — ≤32 chars
- `body` — ≤200 chars
- `tone` — `info|success|alert`
- `icon` — optional
- `image_url` — optional hero image
- `action` — optional single action

**Phase 5 ε1b note (April 2026):** the scheduler service (RFC at
[`docs/RFC-scheduler.md`](RFC-scheduler.md)) is a `widget_card`
producer.  Scheduled reminders fire as `widget_card` frames with
`skill_id="scheduler"` and `card_id="sched_<8hex>"`.  The card
includes `action={"label":"Dismiss","event":"scheduler.dismiss"}`
which the existing SurfaceManager default-dismiss path closes for
free.  No new wire format — Tab5's existing `voice.c:1269`
handler renders these the same as any other card.

### 17.6 List — `widget_list`

```json
{
  "type": "widget_list",
  "skill_id": "cooking.recipe",
  "card_id": "pick_recipe_1",
  "title": "Pick a recipe",
  "items": [
    {"id": "carbonara", "title": "Carbonara", "subtitle": "25 min · pantry"},
    {"id": "ramen",     "title": "Ramen",     "subtitle": "20 min · noodles"},
    {"id": "tacos",     "title": "Tacos",     "subtitle": "30 min · beef"}
  ],
  "on_select": "cooking.chose_recipe"
}
```

Tap row → `widget_action` with `{event: on_select, payload: {id: ...}}`.

### 17.7 Chart — `widget_chart`

```json
{
  "type": "widget_chart",
  "skill_id": "health.mood",
  "card_id": "mood_week",
  "title": "Mood this week",
  "kind": "spark",
  "series": [0.4, 0.5, 0.6, 0.7, 0.6, 0.8, 0.75],
  "unit": "score",
  "range": {"min": 0.0, "max": 1.0}
}
```

**Kinds:**
- `spark` — one-line chart, height ~80px on Tab5
- `bar` — vertical bar grid
- `gauge` — single-value arc

Devices that don't implement `chart` (per capability) get a `widget_card`
fallback with a text body (`"avg: 0.62, trending up"`) synthesized by the
brain-side `SurfaceManager`.

### 17.8 Media — `widget_media`

Alias of §15 `media` with a `skill_id` added so actions can route back to the
owning skill. Same JPEG fetch + TJPGD decode path.

### 17.9 Prompt — `widget_prompt`

```json
{
  "type": "widget_prompt",
  "skill_id": "fitness.logger",
  "card_id": "log_reps_1",
  "question": "How many reps?",
  "input": {"kind": "number", "placeholder": "e.g. 12"},
  "on_answer": "fitness.record_reps"
}
```

**Input kinds:**
- `text` — opens keyboard
- `number` — opens number keyboard
- `choice` — shows list (`items` array required)
- `confirm` — yes/no; voice-only; no keyboard

### 17.10 Dismiss — `widget_dismiss`

Generic dismissal for any non-live widget by card_id.

```json
{"type": "widget_dismiss", "card_id": "mood_week"}
```

### 17.11 Action — `widget_action` (Tab5 → Dragon)

Emitted when user taps an action or answers a prompt.

```json
{
  "type": "widget_action",
  "card_id": "ts_25",
  "event": "ts.pause"
}
```

With payload (from prompt answer or list selection):

```json
{
  "type": "widget_action",
  "card_id": "log_reps_1",
  "event": "fitness.record_reps",
  "payload": {"value": 12}
}
```

Dragon's `widget_action_router` dispatches to the owning skill's
`on_action(event, payload)` handler. Errors → `widget_card` with `tone=alert`.

### 17.12 Capability advertisement — inline in `register` frame

**Wave 14 W14-H19 reconciliation:** this section previously described a
standalone `{"type":"widget_capability",...}` frame sent separately from
`register`. That frame was **never implemented**. In reality Tab5 nests
widget capabilities inside the `register` frame's `capabilities.widgets`
sub-object (see §2.1). Dragon's register handler reads
`capabilities.widgets.types` + the `*_max_*` fields and caches them on
the session; the `SurfaceManager` downgrade path uses those values.

If you're looking for the shape, see §2.1 — it is canonical. The
icon list + separate `render_mode` fields from the old spec are not
plumbed; Dragon assumes `render_mode: "client"` and does not restrict
icons server-side (unknown icons render blank on Tab5 per §17.13).

### 17.13 Error handling

- **Unknown widget type** — client ignores, logs "unknown widget type: X".
- **Unknown icon** — client renders without icon (blank top-right slot).
- **Unknown tone** — treat as `active`.
- **Malformed JSON** — client drops, logs.
- **`widget_action` without matching card_id on Dragon** — logged, dropped.

### 17.14 Backward compatibility

All widget_* messages are new. Devices that don't know them are unchanged.
Existing rich-media `media`, `card`, `audio_clip` continue to work as-is;
widget_card is its superset (adds action).

**When to prefer widget_* vs existing:**
- `widget_card` is preferred for **skill-emitted** cards (has skill_id +
  action); the old `card` is kept for chat-embedded LLM responses (code
  blocks, tool results).
- `widget_media` extends `media` with `skill_id`; use for skill-sourced
  images.

### 17.15 Message type reference (additions to §16)

| Type | Direction | Required Fields | Since |
|---|---|---|---|
| `widget_live` | D→T | `skill_id`, `card_id`, `title`, `body`, `tone` | v1 |
| `widget_live_update` | D→T | `card_id` | v1 |
| `widget_live_dismiss` | D→T | `card_id` | v1 |
| `widget_card` | D→T | `skill_id`, `card_id`, `title`, `body`, `tone` | v1 |
| `widget_list` | D→T | `skill_id`, `card_id`, `title`, `items`, `on_select` | v1 |
| `widget_chart` | D→T | `skill_id`, `card_id`, `title`, `kind`, `series` | v1 |
| `widget_media` | D→T | `skill_id`, `url`, `alt` | v1 |
| `widget_prompt` | D→T | `skill_id`, `card_id`, `question`, `input`, `on_answer` | v1 |
| `widget_dismiss` | D→T | `card_id` | v1 |
| `widget_action` | T→D | `card_id`, `event` | v1 |
| (capability) | inline in `register` | `capabilities.widgets.{types, *_max_*, screen_*}` | v1, see §2.1 |

---

## 18. Video + Call Audio (April 2026)

Two-way video calls between Tab5, Dragon, and a browser client. Dragon is a verbatim broadcast relay — no transcode, no buffering beyond the WS write queue.

Wire framing for both directions:

| Magic (4 bytes ASCII) | Length (4 bytes BE u32) | Payload |
|-----------------------|--------------------------|---------|
| `VID0`                | `len`                    | JPEG frame (Tab5 HW JPEG encoder, web `getUserMedia` MediaRecorder, or `/api/video/inject`) |
| `AUD0`                | `len`                    | Raw 16 kHz mono int16 PCM (Tab5 mic in `VOICE_MODE_CALL`, web Web Audio capture) |

Untagged binary frames remain raw mic PCM bound for STT (existing §3.2 behaviour) — the magic tag is what disambiguates.

**Relay model:** A frame received from one connection is forwarded byte-for-byte to all OTHER connections (sender excluded). Implemented in `dragon_voice/video_upstream.py`.

**Endpoints:**
- `POST /api/video/inject` — debug push of a JPEG into the relay (bearer-auth)
- `GET /call` — serves `dragon_voice/static/call.html`, the browser call client

**Tab5 atomic helpers:** `voice_video_start_call()` / `voice_video_end_call()` flip `VOICE_MODE_CALL`, open/close the camera, and show/hide the video pane in one call.

---

## 19. Progress Event Bus (β-arch, April 2026)

**Added with the β-arch refactor (TinkerBox PR #124, closes issue #123).** A unified `progress` channel that supersedes the per-phase ad-hoc events introduced by Phase 2 (`dictation_postprocessing`, `tool_call`/`tool_result`, etc.).  The vision: one Tab5 renderer for all in-flight signals; new progress phases plug in for free without protocol churn.

### 19.1 Wire format

```json
{
  "type": "progress",
  "phase": "dictation_post" | "tool" | "tts" | "stt" | "llm" | "media_render",
  "stage": "start" | "update" | "done" | "error" | "cancelled",
  "payload": { /* phase-specific dict, optional */ },
  "code": "...",            // present when stage in {error, cancelled}
  "message": "...",         // present when stage in {error, cancelled}
  "severity": "transient" | "fatal",  // present on error stage
  "scope": "..."            // present on error stage; mirrors §8 error frames
}
```

`phase` and `stage` are independent. A long-running phase (LLM, dictation_post, RAG retrieval) emits `start` once, `update` zero or more times, then exactly one of `done` / `error` / `cancelled`. A cheap atomic phase (a single tool call) may emit only `start` then `done`, skipping `update`.

`error` and `cancelled` stages carry the same `code` / `message` taxonomy as the §8 error frames.  `error` additionally carries `severity` + `scope` so Tab5's γ2-H8 routing-by-severity logic applies (TRANSIENT → toast, FATAL → caption + retry).  `cancelled` is semantically distinct — no operator action needed — and omits severity/scope.

### 19.2 Phase enum

| Wire value | Meaning |
|---|---|
| `stt` | Speech-to-text transcription pipeline |
| `llm` | LLM token generation (streamed and full-response) |
| `tts` | Text-to-speech audio synthesis |
| `tool` | Tool-calling — invocation, execution, result return, args-parse failure |
| `dictation_post` | Post-dictation summary generation |
| `media_render` | Rich-media rendering (Pygments, Pillow tables, image fetches) |

### 19.3 Stage enum

| Wire value | Meaning | Tab5 surface action |
|---|---|---|
| `start` | Work begins | Show in-flight UI (spinner, progress) |
| `update` | Partial new info; still working | Refresh in-flight UI; state unchanged |
| `done` | Success | Clear in-flight UI; present `payload` |
| `error` | Failure (carries γ1 taxonomy) | Apply γ2-H8 routing-by-severity |
| `cancelled` | Superseded by a newer request | Clear in-flight UI; no banner |

### 19.4 Migrated channels (this PR)

| Phase | Stage | Legacy event (still emitted in transition) | Notes |
|---|---|---|---|
| `dictation_post` | `start` | `{"type":"dictation_postprocessing"}` | Empty payload |
| `dictation_post` | `done` | `{"type":"dictation_summary","title":...,"summary":...}` | `payload` carries `title`+`summary` |
| `dictation_post` | `error` | `{"type":"dictation_postprocessing_error","error":...,"message":...}` | `code` ← legacy `error` field |
| `dictation_post` | `cancelled` | `{"type":"dictation_postprocessing_cancelled"}` | `code: "dictation_post_cancelled"` |
| `tool` | `start` | `{"type":"tool_call","tool":...,"args":...}` | `payload` nests `tool`+`args` |
| `tool` | `done` | `{"type":"tool_result","tool":...,"result":...,"execution_ms":...}` | `payload` nests result fields |
| `tool` | `error` | §8 error frame `{"code":"tool_args_invalid",...}` | Pair carries identical taxonomy |

### 19.5 Backward compatibility — double-write transition

Migrated emitters in the Dragon server send BOTH the legacy ad-hoc event AND the new `progress` event in that order, gated by the server-side `VoiceConfig.progress_bus_emit_legacy` flag (default `True`).  An unmodified Tab5 silently ignores the new `type: "progress"` frames (the `voice.c` event-handler chain has no terminal panic for unknown types).  Once Tab5 ships a `progress`-aware build, set the flag to `False` to drop the legacy emit lines in a follow-up cleanup PR.

### 19.6 Not yet migrated (deferred to future PRs)

`stt`, `llm`, `tts`, and `media_render` phases are intentionally out of scope for this PR — they're deeply wired into Tab5's audio + chat-bubble rendering and need more careful migration. Their phase entries are reserved in the enum so tooling that switches on `Phase` doesn't break when they land.

---

## 20. Channel Messaging (W7-E / W7-F)

The channel-messaging surface lets a third-party messaging platform (Telegram, WhatsApp, Discord, etc.) deliver a message to Tab5 via Dragon, and lets the user reply back through the same path.  W7-E (Tab5 side) implements the rendering + reply UI; W7-F (Dragon side) implements the WS plumbing.  Real platform connectors land later (W7-F.2 — Python WS-RPC client to OpenClaw gateway).

### 20.1 channel_message (Dragon -> Tab5)

```json
{
  "type": "channel_message",
  "channel": "tg",
  "message_id": "tg:8675309:42",
  "thread_id": "tg:8675309",
  "sender": {"display_name": "Jamie Park", "starred": true},
  "text": "Are you still on for lunch Thursday?",
  "preview": "Are you still on for lunch Thursday?",
  "ts": 1715568000,
  "priority": "high",
  "needs_reply": true,
  "metadata": {"platform_thread_url": "https://t.me/c/..."}
}
```

| Field | Type | Description |
|---|---|---|
| `type` | string | `"channel_message"` |
| `channel` | string | Short canonical platform name (`tg`, `wa`, `dc`, `sl`, `sg`, `im`, `ma`, `em`).  Maps to NVS `ch_*_on` toggle on Tab5 (see TinkerTab CLAUDE.md). |
| `message_id` | string | Globally unique id; Tab5 dedupes against a 32-entry PSRAM ring. |
| `thread_id` | string | Used for reply routing (carried back in `channel_reply.thread_id`). |
| `sender.display_name` | string | Sender name surfaced in toast + now-card kicker. |
| `sender.starred` | bool | If true, Tab5 routes the message to the now-card surface regardless of `priority`. |
| `text` | string | Full message body. |
| `preview` | string | Short version for the toast surface; falls back to `text` when absent. |
| `priority` | string | `low` / `normal` / `high`.  `high` → now-card; lower → toast. |
| `needs_reply` | bool | Force now-card routing.  Implies user wants to react. |
| `metadata` | object | Free-form; Tab5 ignores unknown fields. |

**Tab5 behavior:**
1. Drop if `channel` is set AND `tab5_settings_get_channel_enabled(channel)` is false (Settings → CHANNELS toggle).
2. Drop if `message_id` already seen in the 32-entry dedupe ring.
3. Route to now-card (priority=high OR sender.starred OR needs_reply) or toast.
4. Fire `UI_CUE_INCOMING_HIGH` / `UI_CUE_INCOMING_LOW` unless quiet-hours active.

### 20.2 channel_reply (Tab5 -> Dragon)

```json
{
  "type": "channel_reply",
  "channel": "tg",
  "thread_id": "tg:8675309",
  "text": "yes lunch thurs works, 12:30 ok?",
  "in_reply_to": "tg:8675309:42"
}
```

| Field | Type | Description |
|---|---|---|
| `type` | string | `"channel_reply"` |
| `channel` | string | Mirrors the inbound `channel`. |
| `thread_id` | string | Mirrors the inbound `thread_id`. |
| `text` | string | The user's reply text — either a voice-dictated transcript (W7-E.4b) or programmatic quick-ack (W7-E.4 legacy). |
| `in_reply_to` | string | Optional `message_id` the user is replying to. |

**Dragon behavior (W7-F stub):**
1. Log the receive.
2. Emit `channel_reply_ack` with `ok=true` and a `stub:<hex>` `platform_message_id`.
3. Real platform forwarding (Telegram send-message API etc.) is W7-F.2 — the stub closes the round-trip so Tab5's "Replied via X" toast fires naturally.

### 20.3 channel_reply_ack (Dragon -> Tab5)

```json
{
  "type": "channel_reply_ack",
  "channel": "tg",
  "thread_id": "tg:8675309",
  "ok": true,
  "platform_message_id": "tg:8675309:43"
}
```

| Field | Type | Description |
|---|---|---|
| `type` | string | `"channel_reply_ack"` |
| `channel` | string | Mirrors the reply's `channel`. |
| `thread_id` | string | Mirrors the reply's `thread_id`. |
| `ok` | bool | True on successful delivery (or stub-acknowledged). |
| `platform_message_id` | string | Platform-assigned message id on success; `stub:<hex>` from the W7-F stub. |
| `error` | string | Present when `ok=false`; surfaced as a Tab5 toast. |

**Tab5 behavior:**
1. On `ok=true` → toast "Replied via {channel}" + obs `ui.notif.reply ack_ok`.
2. On `ok=false` → toast "Reply failed: {error}" + obs `ui.notif.reply ack_fail`.

