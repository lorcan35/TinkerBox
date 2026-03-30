# TinkerClaw WebSocket Protocol v1

**Version:** 1.0.0
**Date:** 2026-03-30
**Status:** Draft — both repos must implement against this spec.

This document defines the WebSocket protocol between **TinkerTab** (Tab5 ESP32-P4 firmware) and **TinkerBox** (Dragon Q6A server). TinkerTab is the thin client. TinkerBox is the brain.

**Endpoint:** `ws://<dragon-ip>:3502/ws/voice`

---

## 1. Connection Lifecycle

```
Tab5                                    Dragon
  |                                       |
  |--- WS CONNECT /ws/voice ------------>|
  |                                       |
  |--- register (JSON) ----------------->|  Device registration
  |<-- session_start (JSON) -------------|  Session assignment
  |                                       |
  |  ... conversation ...                 |
  |                                       |
  |--- WS CLOSE ----------------------->|  Disconnected
  |                                       |  Session → PAUSED
  |                                       |
  |--- WS CONNECT /ws/voice ----------->|  Reconnect
  |--- register (JSON, session_id) ----->|  Session resume
  |<-- session_start (JSON) -------------|  Same session, history intact
```

### 1.1 Device Registration (Tab5 → Dragon)

**MUST be the first text frame after WebSocket connect.**

```json
{
  "type": "register",
  "device_id": "aabbccddeeff",
  "hardware_id": "AA:BB:CC:DD:EE:FF",
  "name": "Living Room Tab5",
  "firmware_ver": "0.4.2",
  "platform": "esp32p4-tab5",
  "capabilities": {
    "mic": true,
    "speaker": true,
    "screen": true,
    "camera": true,
    "sd_card": true,
    "touch": true
  },
  "session_id": null
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| type | string | yes | Must be `"register"` |
| device_id | string | yes | Persistent UUID from NVS. Generated once on first boot, never changes. |
| hardware_id | string | yes | MAC address or hardware serial. Immutable. |
| name | string | no | User-friendly device name. Default empty. |
| firmware_ver | string | yes | Firmware version string. |
| platform | string | yes | Device type identifier. |
| capabilities | object | yes | What the device can do. |
| session_id | string | null | If resuming a previous session, send the session_id. Null for new session. |

### 1.2 Session Start (Dragon → Tab5)

Sent immediately after registration is processed.

```json
{
  "type": "session_start",
  "session_id": "a1b2c3d4e5f6",
  "device_id": "aabbccddeeff",
  "resumed": false,
  "message_count": 0,
  "config": {
    "stt": "moonshine",
    "tts": "piper",
    "llm": "npu_genie",
    "tts_sample_rate": 22050,
    "response_mode": "match_input",
    "system_prompt": "You are Tinker..."
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| type | string | `"session_start"` |
| session_id | string | Assigned or resumed session ID. Tab5 stores this in NVS. |
| device_id | string | Echo of the registered device ID. |
| resumed | bool | True if this is a resumed session with existing history. |
| message_count | int | Number of messages in the resumed session (0 for new). |
| config | object | Active config for this session. Tab5 uses this for local settings. |

---

## 2. Message Types

### 2.1 Frame Types Summary

| Direction | Frame | Type | Description |
|-----------|-------|------|-------------|
| Tab5 → Dragon | Binary | — | Raw PCM audio: int16, 16kHz, mono |
| Tab5 → Dragon | JSON | `register` | Device registration (section 1.1) |
| Tab5 → Dragon | JSON | `start` | Begin new voice turn / reset audio buffer |
| Tab5 → Dragon | JSON | `stop` | End of speech — process audio now |
| Tab5 → Dragon | JSON | `cancel` | Abort current processing |
| Tab5 → Dragon | JSON | `text` | Text input (keyboard/API) |
| Tab5 → Dragon | JSON | `record_start` | Begin recording mode (for notes) |
| Tab5 → Dragon | JSON | `record_stop` | End recording — process as note |
| Tab5 → Dragon | JSON | `ping` | Application-level heartbeat (see LEARNINGS.md #11) |
| Tab5 → Dragon | JSON | `config_ack` | Acknowledge a config_update from Dragon |
| Dragon → Tab5 | Binary | — | TTS audio: PCM int16 16kHz mono (resampled from TTS engine rate) |
| Dragon → Tab5 | JSON | `session_start` | Session assignment (section 1.2) |
| Dragon → Tab5 | JSON | `stt` | Transcription result |
| Dragon → Tab5 | JSON | `llm` | LLM response text (may stream) |
| Dragon → Tab5 | JSON | `tts_start` | TTS audio stream beginning |
| Dragon → Tab5 | JSON | `tts_end` | TTS audio stream complete |
| Dragon → Tab5 | JSON | `note_created` | Note created from recording |
| Dragon → Tab5 | JSON | `config_update` | Dragon pushes config change |
| Dragon → Tab5 | JSON | `error` | Error message |
| Dragon → Tab5 | JSON | `event` | Generic system event |

### 2.2 Voice Input Flow (Tab5 → Dragon)

```
Tab5 sends: {"type": "start"}           — clears audio buffer
Tab5 sends: [binary PCM frames...]      — raw audio data
Tab5 sends: {"type": "stop"}            — triggers processing
Dragon sends: {"type": "stt", "text": "What's the weather?"}
Dragon sends: {"type": "llm", "text": "It's sunny..."}
Dragon sends: {"type": "tts_start"}
Dragon sends: [binary TTS audio frames...]
Dragon sends: {"type": "tts_end"}
```

### 2.3 Text Input (Tab5 → Dragon)

```json
{
  "type": "text",
  "content": "What's the weather like?"
}
```

Skips STT. Goes directly to conversation engine. Response routing depends on `config.response_mode`:
- `"match_input"` — text input gets text-only response (no TTS)
- `"always_speak"` — all responses get TTS audio
- `"always_text"` — all responses are text-only (no TTS)

Dragon responds with the same `stt` → `llm` → `tts_start/end` flow, except `stt` is skipped for text input.

### 2.4 Recording Mode (Tab5 → Dragon)

```json
{"type": "record_start"}
// Tab5 sends binary PCM frames...
{"type": "record_stop"}
```

Dragon processes the recording as a note (STT → summarize → embed → store). Response:

```json
{
  "type": "note_created",
  "note": {
    "id": "abc123",
    "title": "Meeting about Q2 goals",
    "summary": "Discussion about...",
    "word_count": 342,
    "duration_s": 127.5
  }
}
```

### 2.5 Config Sync (Dragon → Tab5)

Dragon can push config changes at any time:

```json
{
  "type": "config_update",
  "config": {
    "tts_sample_rate": 22050,
    "response_mode": "always_speak"
  }
}
```

Tab5 applies relevant settings (sample rate, response mode, etc.) and ACKs:

```json
{
  "type": "config_ack",
  "applied": ["tts_sample_rate", "response_mode"]
}
```

### 2.6 Events (Dragon → Tab5)

Generic event push for UI notifications, status updates, etc:

```json
{
  "type": "event",
  "event": "skill.completed",
  "data": {
    "skill": "weather",
    "result": "Sunny, 28°C"
  }
}
```

### 2.7 Error (Dragon → Tab5)

```json
{
  "type": "error",
  "code": "stt_failed",
  "message": "Transcription failed: model not loaded"
}
```

Error codes: `stt_failed`, `llm_failed`, `tts_failed`, `session_invalid`, `rate_limited`, `internal`.

---

## 3. Audio Format

| Parameter | Value |
|-----------|-------|
| Encoding | PCM signed 16-bit little-endian |
| Sample rate (Tab5 → Dragon) | 16000 Hz |
| Channels | 1 (mono) |
| Sample rate (Dragon → Tab5) | 16000 Hz (Dragon resamples from TTS engine rate before sending) |
| Tab5 hardware rate | 48000 Hz (Tab5 resamples internally) |

Tab5 captures at 48kHz from ES7210, downsamples 3:1 to 16kHz before sending.
Dragon resamples TTS output (e.g. 22050 Hz from Piper) to 16kHz before sending.
Tab5 receives 16kHz TTS audio, upsamples to 48kHz for ES8388 DAC playback.

---

## 4. Session Persistence

- **Session ID** is stored in Tab5 NVS after `session_start`.
- On reconnect, Tab5 sends `session_id` in the `register` message.
- Dragon checks if session exists and is in `paused` status → resumes it.
- If session doesn't exist or is `ended` → creates new session.
- Dragon sets session to `paused` on WebSocket disconnect (not `ended`).
- Session auto-ends after configurable timeout (default: 30 minutes of inactivity).
- All messages are preserved in the session — full conversation history survives reconnects.

---

## 5. Device Identity

- `device_id` is a UUID generated once on first boot, stored in NVS.
- `hardware_id` is the MAC address (immutable, identifies the physical device).
- Dragon uses `device_id` as the primary key. `hardware_id` is for dedup/recovery.
- If a Tab5 is factory-reset (new `device_id`), Dragon matches by `hardware_id` and migrates.
- Multiple devices can connect simultaneously — each gets its own session.

---

## 6. Versioning

Protocol version negotiation is implicit via `firmware_ver` in the registration message. Dragon checks firmware version and adjusts behavior if needed. Future breaking changes will increment the protocol version and require explicit negotiation.
