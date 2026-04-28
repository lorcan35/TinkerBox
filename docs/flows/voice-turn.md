# Voice Turn — End-to-End Trace

> Concrete walkthrough of what happens when a user taps the home-screen
> orb on Tab5 and asks "what time is it?".  Each step has a file:line
> reference so you can chase the actual code.  When an answer arrives
> through the speaker ~3-90 seconds later, all 18 of these steps have
> happened.

## Setup

- Tab5 boot: WS connected to Dragon, `voice.state == READY`.
- Voice mode: 0 (Local) — STT=Moonshine, LLM=ollama ministral-3:3b, TTS=Piper.
- User on home screen, looking at the Tinker orb.

## The trace

```mermaid
sequenceDiagram
    participant U as 👤 User
    participant T as Tab5
    participant D as Dragon
    participant O as Ollama (LLM)

    U->>T: Tap orb
    T->>T: ui_voice_show() + voice_start_listening()
    T->>D: WS {"type":"start"}
    Note over T: VOICE_STATE_LISTENING<br/>orb breathes, mic capturing
    U->>T: "what time is it?"
    T->>D: binary PCM frames (16 kHz int16)
    U->>T: Tap orb again (or auto-stop on silence)
    T->>D: WS {"type":"stop"}

    Note over D: VAD declared end-of-utterance
    D->>D: Moonshine STT → "what time is it?"
    D->>T: WS {"type":"stt","text":"..."}
    D->>D: ConversationEngine.process_text_stream
    D->>D: build_context (memory + tool descriptions)
    D->>O: POST /api/chat (ministral-3:3b)
    O->>D: token stream
    D->>T: WS {"type":"llm","text":"<token>"} (×N)

    Note over D: First tool marker detected:<br/>&lt;tool&gt;datetime&lt;/tool&gt;
    D->>D: ToolRegistry.execute("datetime")
    D->>T: WS {"type":"tool_call","tool":"datetime",...}
    D->>O: POST /api/chat (with tool result injected)
    O->>D: final tokens
    D->>T: WS {"type":"llm_done","llm_ms":1234}
    D->>T: WS {"type":"tts_start","sample_rate":16000}
    D->>T: binary PCM TTS audio (×N chunks)
    D->>T: WS {"type":"tts_end"}
    T->>U: Speaker plays "It's 3:42 PM"
    Note over T: VOICE_STATE_READY (orb idle)
```

### Step-by-step

#### 1. User taps orb on home screen
[`main/ui_home.c:1261`](https://github.com/lorcan35/TinkerTab/blob/main/main/ui_home.c#L1261) — `cb_orb_speak_press` fires, calls `ui_voice_show()` then `voice_start_listening()`.

The orb is in `lv_layer_top()` so it works on any home page.  Halo
ring animation continues while voice flow runs.  ([`ui_voice.c:611`](https://github.com/lorcan35/TinkerTab/blob/main/main/ui_voice.c#L611))

#### 2. Tab5 transitions LISTENING and starts mic capture
[`main/voice.c:355`](https://github.com/lorcan35/TinkerTab/blob/main/main/voice.c#L355) — `voice_set_state(LISTENING)`. The state callback updates the home orb tint, fires `tab5_debug_obs_event("voice.state", "LISTENING")` for the e2e harness, and the persistent mic task ([`voice.c:2400+`](https://github.com/lorcan35/TinkerTab/blob/main/main/voice.c#L2400)) wakes via its event semaphore.

The mic task pulls 4-channel TDM samples from the ES7210 ADC at 48 kHz, downsamples 3:1 to 16 kHz, extracts the slot-0 (primary mic) channel, and chunks them at 30 ms boundaries.

#### 3. Tab5 sends `{"type":"start"}` on WS
[`main/voice.c:voice_start_listening`](https://github.com/lorcan35/TinkerTab/blob/main/main/voice.c) sends a JSON text frame: `{"type":"start"}`. (For dictation mode it adds `"mode":"dictate"` — see `dictation` doc.)

Dragon's WS handler ([`dragon_voice/server.py:752`](https://github.com/lorcan35/TinkerBox/blob/main/dragon_voice/server.py#L752)) receives it via the dispatcher and arms the pipeline for an utterance.

#### 4. Tab5 streams binary mic frames
Untagged binary WS frames carry raw 16 kHz mono int16 PCM. Each frame is one ~30 ms chunk (~960 bytes). On Dragon, [`server.py: _on_audio`](https://github.com/lorcan35/TinkerBox/blob/main/dragon_voice/server.py) feeds them into the pipeline's circular buffer. The pipeline's VAD ([`pipeline.py: VoicePipeline._on_chunk`](https://github.com/lorcan35/TinkerBox/blob/main/dragon_voice/pipeline.py)) tracks energy + silence duration to decide end-of-utterance.

> **Why untagged?** Legacy mic path predates the VID0/AUD0 magic prefix; `AUD0`-tagged binary is reserved for `VOICE_MODE_CALL` where the audio bypasses STT.

#### 5. User stops talking → silence triggers `stop`
Either VAD detects 600 ms of silence and Tab5 auto-stops, or the user taps the orb again. Either way: `{"type":"stop"}` is sent. ([`voice.c:voice_stop`](https://github.com/lorcan35/TinkerTab/blob/main/main/voice.c))

Voice state on Tab5 transitions to `PROCESSING`; orb dims to indicate "thinking".

#### 6. Dragon runs Moonshine STT
[`dragon_voice/stt/moonshine_stt.py: transcribe`](https://github.com/lorcan35/TinkerBox/blob/main/dragon_voice/stt/moonshine_stt.py) — the buffered audio is fed to `moonshine_voice` (medium model, streaming variant). On Dragon Q6A this takes ~400-800 ms for a 2-second utterance.

A `{"type":"stt","text":"what time is it?"}` event goes back to Tab5 so the chat overlay can show what was heard.

#### 7. ConversationEngine takes over
[`dragon_voice/conversation.py: process_text_stream`](https://github.com/lorcan35/TinkerBox/blob/main/dragon_voice/conversation.py) — the canonical text-turn entry point. Even voice turns end up here once STT is done; vision turns also enter here post-#186.

The engine first persists the user message via [`messages.py: MessageStore.add_message`](https://github.com/lorcan35/TinkerBox/blob/main/dragon_voice/messages.py) (append-only — `id, session_id, role:"user", content, input_mode:"voice", created_at`).

#### 8. Build context: memory + tools + history
[`conversation.py: _build_context`](https://github.com/lorcan35/TinkerBox/blob/main/dragon_voice/conversation.py) does three things:
1. Loads recent message history (10 for local backends, 30 for cloud) via `MessageStore.get_context(media_store=...)`. Multimodal markers (`__mm__:` content prefix) hydrate to OpenAI `image_url` content arrays here.
2. Asks `MemoryService.get_relevant_context(user_text)` for relevant facts (cosine-similarity search over nomic-embed-text 768-dim vectors). Top hits are appended to the system prompt.
3. Renders the tool registry into the system prompt — compact format for local models, full for cloud (`registry.format_for_llm(compact=is_local)`).

A token-budget trim runs over the assembled context (25.6K for local, 100K for cloud) — see [`messages.py: trim_context_to_budget`](https://github.com/lorcan35/TinkerBox/blob/main/dragon_voice/messages.py).

#### 9. Hand context to the LLM (or router)
With `backend: "router"`, `self._llm` is a `CapabilityAwareRouter`. [`router.py: generate_stream_with_messages`](https://github.com/lorcan35/TinkerBox/blob/main/dragon_voice/llm/router.py) runs `infer_required_caps(messages)` (text-only here — no images), picks the lowest-priority spec in the active tier (`ministral` priority 0 in local), lazily instantiates the sub-backend if needed, and delegates.

For the legacy single-backend case, `self._llm` is just `OllamaBackend` directly.

#### 10. Ollama call
[`ollama_llm.py: generate_stream_with_messages`](https://github.com/lorcan35/TinkerBox/blob/main/dragon_voice/llm/ollama_llm.py) translates content arrays to Ollama format (a no-op for text-only turns), POSTs `/api/chat` to `localhost:11434`, and async-iterates the stream of NDJSON chunks.

Each chunk's `message.content` field is a token; the keepalive parameter (10 minutes for ministral, much shorter for vision models per fleet config) keeps the model resident between calls.

#### 11. Token streaming with marker-aware buffering
[`conversation.py: 376-388`](https://github.com/lorcan35/TinkerBox/blob/main/dragon_voice/conversation.py#L376) — tokens flow back to Tab5 as `{"type":"llm","text":"<token>"}` via the WS unless they look like the start of a tool marker (`<tool>` or `<tool_call>`). The rolling buffer holds tokens at marker boundaries; benign prose flushes immediately so the user sees text accumulate in real time.

WebSocket keepalive pings at 5 s intervals via [`server.py: _ws_keepalive_during_inference`](https://github.com/lorcan35/TinkerBox/blob/main/dragon_voice/server.py) prevent Tab5's PONG-watch from tripping during a 60-90 s local inference. Without this, Tab5 would reconnect mid-stream and Dragon's P13 "device already has connection" guard would evict the in-flight LLM.

#### 12. Tool detected: datetime
The model emits `<tool>datetime</tool><args>{}</args>`. Buffer hits the marker boundary. [`tools/registry.py: parse_tool_call`](https://github.com/lorcan35/TinkerBox/blob/main/dragon_voice/tools/registry.py) handles three accepted dialects (legacy `<tool>`, standard `<tool_call>`, bracketed `[NAME]`) and tolerates the small-model XML quirks documented in PR #74/#79.

A `{"type":"tool_call","tool":"datetime","args":{}}` event goes to Tab5 — chat overlay shows "Looking up time…" thinking bubble.

#### 13. Tool execution
[`tools/datetime_tool.py`](https://github.com/lorcan35/TinkerBox/blob/main/dragon_voice/tools/datetime_tool.py) returns the current local time. The result is appended to the conversation as a `tool` role message (so it's in the next prompt's context).

A `{"type":"tool_result","tool":"datetime","result":...,"execution_ms":2}` event reaches Tab5; the thinking bubble closes.

#### 14. Second LLM call with the tool result
ConversationEngine loops back to step 9 with the tool result injected. `MAX_TOOL_CALLS=3` per turn caps runaway agents; if hit, an info event `tool_call_limit` surfaces to Tab5 as a toast.

The model's second response: "It's 3:42 PM."

#### 15. Empty-reply guard (#75/#79)
Some FC-trained models emit a tool call and stop, leaving no user-visible text. [`tools/response_wrap.py: synthesize_wrap`](https://github.com/lorcan35/TinkerBox/blob/main/dragon_voice/tools/response_wrap.py) catches this and synthesizes a one-line natural-language ack from the tool result. Triggered when `looks_like_useful_text(response)` returns False AND at least one tool fired.

#### 16. `llm_done` + persist assistant response
`{"type":"llm_done","llm_ms":1234}` goes to Tab5. [`messages.py: add_message(role="assistant", ...)`](https://github.com/lorcan35/TinkerBox/blob/main/dragon_voice/messages.py) persists the final response.

The Tab5-side `chat.llm_done` debug-obs event fires here too — the e2e harness uses this to know an LLM cycle finished.

#### 17. TTS pipeline
[`tts/piper_tts.py: synthesize_stream`](https://github.com/lorcan35/TinkerBox/blob/main/dragon_voice/tts/piper_tts.py) — Piper en_US-lessac-medium runs on Dragon CPU at ~real-time (~1× factor). Output is 22050 Hz mono int16 PCM, resampled to 16 kHz before sending to Tab5.

`{"type":"tts_start","sample_rate":16000}` → binary PCM chunks (4-32 KB) → `{"type":"tts_end"}`.

In Cloud mode the TTS comes from OpenRouter's `gpt-audio-mini` at 24 kHz instead; the resample step targets 16 kHz uniformly.

#### 18. Tab5 plays back
[`main/voice.c: voice_play`](https://github.com/lorcan35/TinkerTab/blob/main/main/voice.c) feeds binary frames into the playback ring buffer. A drain task dequeues into `esp_codec_dev_write()` to the ES8388 DAC, upsampling 1:3 from 16 kHz to 48 kHz to match the I2S hardware rate.

Voice state transitions: `PROCESSING` → `SPEAKING` (on `tts_start`) → `READY` (on `tts_end`). Orb idles. The full conversation is now in `messages` table for resume.

## Latency budget

Typical Q6A ARM64 timings for "what time is it?":

| Step | Local mode | Cloud mode |
|------|-----------|-----------|
| Mic capture (utterance length) | ~2 s | ~2 s |
| Network: Tab5 → Dragon | ~5 ms | ~5 ms |
| Moonshine STT | ~600 ms | (cloud STT: ~500 ms) |
| ConversationEngine + memory + tool prompt | ~50 ms | ~50 ms |
| LLM first call (incl. tool call) | **~30 s** ministral | ~600 ms haiku-3 |
| Tool execution (datetime) | ~2 ms | ~2 ms |
| LLM second call | ~25 s | ~400 ms |
| Piper TTS | ~600 ms (real-time) | (cloud TTS: ~400 ms) |
| Network + Tab5 playback prebuffer | ~150 ms | ~150 ms |
| **Total** | **~58 s** | **~2-3 s** |

Local mode is brutal on Q6A — ministral-3:3b takes ~25-90 s/turn under load (the e2e harness allows 180 s before timing out). The NPU Genie path runs Llama 3.2 1B at ~8 tok/s but is currently text-only and much smaller, so it's a different quality tradeoff.

## Where things go wrong

| Symptom | Cause | Fix / mitigation |
|---------|-------|------------------|
| Tab5 reconnects mid-stream, sees no reply | Local LLM > 30 s, no keepalive pings | `_ws_keepalive_during_inference` (#76) |
| LLM emits tool call but no visible reply | Some FC-trained models drop user text after tool | `synthesize_wrap` (#77/#79) |
| Stale tool list — model never sees `weather`, `note`, etc. | Compact format hardcoded for local models | Per-PR fix; see [`historical/AUDIT-WAVE-14.md`](../historical/AUDIT-WAVE-14.md) |
| Tab5 shows "Disconnected" mid-LLM | WiFi blip; WS reconnect tries ngrok first when `conn_m=0` | Auto-reconnect with exponential backoff |
| Memory context wrong — recalls another user's facts | Per-connection deep-copy of config (#) | See [`historical/AUDIT-WAVE-14.md`](../historical/AUDIT-WAVE-14.md) |
| `chat.llm_done` event missed by harness | Cursor-stealing in events polling | `Tab5Driver.events(peek=True)` (#295) |

## Variations on this flow

- **Text turn:** skip steps 1-6. Tab5 sends `{"type":"text","content":"..."}` directly. ConversationEngine entry is the same.
- **Dictation:** step 3 sends `{"mode":"dictate"}`. STT emits `stt_partial` events as you speak. End-of-utterance is 5 s of silence (`DICTATION_AUTO_STOP_FRAMES=250`). Post-process generates a title + summary.
- **Vision turn:** see [`vision-turn.md`](vision-turn.md) — the photo enters the conversation, the router picks a vision-capable model.
- **Call:** see [`video-call.md`](video-call.md) — bidirectional audio bypasses STT entirely.
- **TinkerClaw mode (3):** ConversationEngine is bypassed. The transcript goes straight to the gateway via `tinkerclaw_llm.py`; the gateway's agent runtime owns tool execution.

## Related docs

- [`../ARCHITECTURE.md`](../ARCHITECTURE.md) — the system at altitude
- [`../protocol.md`](../protocol.md) — full WS field-by-field reference
- [`../router-cookbook.md`](../router-cookbook.md) — fleet-config recipes
- [`../../GLOSSARY.md`](../../GLOSSARY.md) — terms used here
- [`vision-turn.md`](vision-turn.md), [`video-call.md`](video-call.md) — sibling flows
