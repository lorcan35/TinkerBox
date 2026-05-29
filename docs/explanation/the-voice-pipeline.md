---
audience: developer
type: explanation
prerequisites: [How the stack fits together](how-the-stack-fits-together.md), [Voice modes reference](../reference/voice-modes.md)
last-verified: 2026-05-29
---
# How the voice pipeline works

## The question

You speak to a Tab5. A few seconds later it speaks back. In between, a single
object on the Dragon — `VoicePipeline` in
[`dragon_voice/pipeline.py`](../../dragon_voice/pipeline.py) — takes your raw
microphone PCM, turns it into text, runs that text through a multi-turn LLM that
can call tools, synthesizes the reply, and streams audio back. This page builds
the mental model of that orchestration: what each stage does, why the stages are
ordered the way they are, and why several non-obvious mechanisms exist
(sentence-buffered TTS, the WebSocket keepalive that fires during inference,
auto-fallback, and the sample-rate conversions on both ends).

This is the *why*. For the *what* — the exact wire frames — read the
[WebSocket protocol reference](../reference/websocket-protocol.md); for the
backend mapping per mode, read the [voice modes reference](../reference/voice-modes.md);
for the canonical full spec, read [`docs/protocol.md`](../protocol.md).

## The model

A voice turn is a one-directional flow with two re-rate steps bracketing it. The
[Tab5](../../GLOSSARY.md) captures audio, the [Dragon](../../GLOSSARY.md) does
all the thinking, and audio comes back. Everything between the two re-rate steps
runs on the Dragon inside `pipeline.py`.

```text
Mic audio (PCM int16, 16 kHz mono)              ← Tab5 downsamples 48k → 16k (3:1 box filter)
  │
  ▼
[VAD]  energy-based silence detection           ← optional, server-side
  │
  ▼
[STT]  Moonshine / Whisper.cpp / Vosk / OpenRouter
  │      → emits `stt` frame: {"text": "...", "stt_ms": N}
  ▼
[ConversationEngine]  builds context from message history + memory facts
  │                    + document chunks, then calls the LLM
  ▼
[LLM]  streaming tokens (lmstudio / ollama / npu_genie / openrouter / router)
  │      → tool-call markers parsed + executed mid-stream, result re-injected
  │      → emits `llm` token frames, then `llm_done`
  ▼
[Sentence buffer]  flush at sentence boundaries, not at end of generation
  │
  ▼
[TTS]  per-sentence synthesis (Piper / Kokoro / Edge TTS / OpenRouter)
  │
  ▼
[Resample]  TTS engine rate (e.g. 22 050 Hz) → 16 kHz
  │
  ▼
[Pace & stream]  4096-byte chunks, paced at ~80% real-time
  │      → `tts_start`, [binary PCM frames], `tts_end`
  ▼
Speaker audio (PCM int16, 16 kHz mono)          → Tab5 upsamples 16k → 48k (linear interp)
```

### Stage 1 — VAD and capture

The Tab5 streams raw 16 kHz mono int16 PCM as untagged binary WebSocket frames.
The pipeline appends them to a per-connection buffer. Whether a turn ends is
decided in one of two places:

- **Explicit endpointing.** The Tab5 sends `{"type":"start"}`, streams PCM, then
  sends `{"type":"stop"}`. This is the push-to-talk path and the default.
- **Server-side VAD.** When `audio.vad_enabled` is true, the pipeline watches
  input energy and triggers STT after `audio.vad_silence_ms` (default 600 ms) of
  silence — useful when the client cannot detect end-of-speech itself.

The two are not exclusive: VAD is energy-based silence detection layered under
the explicit `start`/`stop` markers. Turn it off (`vad_enabled: false`) when the
client owns endpointing and you do not want the server second-guessing it.

### Stage 2 — STT

The buffered PCM goes to the active STT backend, selected by the
[voice mode](../reference/voice-modes.md): Moonshine on the Q6A CPU for Local
mode, OpenRouter `gpt-audio-mini` for Hybrid/Cloud. The transcript is emitted as
an `stt` frame so the Tab5 can show what it heard before the answer arrives. In
**dictate mode** the flow stops here — see *Dictation* below.

### Stage 3 — the ConversationEngine and the LLM

Voice, text, and vision turns all converge on `ConversationEngine`
(`dragon_voice/conversation.py`). It is not a thin LLM wrapper. Before each call
it builds context from the append-only message history, then injects relevant
memory facts and document chunks found by cosine-similarity search against the
turn's embedding. The result is a memory-augmented prompt, sent to whichever LLM
backend the mode binds.

The LLM streams tokens. Two things happen to that stream:

- **Tool-calling, inline.** When the model emits a tool marker —
  `<tool>name</tool><args>{...}</args>` is the Dragon-standard dialect, with two
  other dialects accepted — the engine parses it, executes the tool, injects the
  result back into the context, and lets the model continue. A turn is capped at
  **three tool calls** so a confused small model cannot loop forever. The token
  stream the client sees never contains the raw `<tool>` markup; it is stripped
  before the `llm` frames go out.
- **The empty-reply guard.** Some function-calling-tuned models fire a tool and
  then stop, leaving no user-visible prose. When that happens *and* at least one
  tool fired, Dragon synthesizes a one-line natural-language acknowledgement from
  the tool result using a per-tool template library — no extra LLM round-trip.
  See `dragon_voice/tools/response_wrap.py`.

When the model finishes, the engine emits `llm_done`.

### Stage 4 — sentence-buffered TTS

The pipeline does **not** wait for the full reply before synthesizing speech. As
LLM tokens arrive, a buffer accumulates them and flushes at sentence boundaries.
Each completed sentence is synthesized and streamed while the LLM is still
generating the next one. This is the single biggest perceived-latency win in the
whole flow: the user hears the first sentence seconds before the model has
finished thinking the last one.

TTS runs on the backend the mode binds — Piper for Local (the default Dragon
engine, `en_US-lessac-medium`), OpenRouter `gpt-audio-mini` for Cloud. Kokoro
and Edge TTS are alternatives.

### Stage 5 — resample, pace, and stream

The TTS engine does not emit at the Tab5's rate, so the pipeline always
resamples to 16 kHz before sending (see *Audio rates* below). Then it streams the
PCM back in **4096-byte chunks paced at roughly 80% of real-time playback
speed**. Pacing matters: if the Dragon dumped audio as fast as TCP allowed, the
Tab5's fixed-size ring buffer would overflow and playback would stutter. By
delivering slightly *slower* than the speaker consumes, the buffer stays
comfortably fed without overrunning. The first few chunks go out immediately so
playback starts fast; the rest are paced. The stream is bracketed by `tts_start`
and `tts_end` frames.

### Dictation: STT without the LLM

Dictation is a different shape of the same machine. The Tab5 sends
`{"type":"start","mode":"dictate"}`, performs **client-side VAD** with adaptive
thresholds, and sends a `segment` marker on each detected pause. Dragon
transcribes each segment independently and returns an `stt_partial` frame per
segment, which the Tab5 appends space-separated into a growing transcript.
Server-side VAD, the LLM, and TTS are all bypassed during the recording —
recording length is unbounded.

When the user stops, the **post-processing** step runs:
`pipeline._post_process_dictation()` sends the assembled transcript to the LLM
*once* to generate a title and summary, then emits a `dictation_summary` frame.
This is the only place dictation touches the LLM, and it happens after the
recording is already saved — so a post-processing failure never costs you the
transcript.

### The keepalive that fires during inference

Local-mode 4B-class models on the Q6A routinely take **60–90 seconds** to finish
a turn. That collides with a hard constraint: the Tab5's WebSocket library
treats a long silence as a dead connection and reconnects — and a reconnect trips
`server.py`'s "device already has connection" guard, which **evicts the in-flight
LLM stream**. Without intervention, every slow Local turn returned an empty
reply.

The fix is a keepalive context manager,
`server.py: _ws_keepalive_during_inference` (issue #76). While the
ConversationEngine is generating, it fires a WebSocket ping every **5 seconds** —
far under any client's connection-watch window — so the socket never looks idle.
It wraps the three slow paths: the TinkerClaw text bypass, the Local text path
through the ConversationEngine, and the vision/multimodal path.

There is a deliberate subtlety on the Tab5 side, documented in
[`docs/protocol.md`](../protocol.md): a keepalive ping does **not** reset the
Tab5's response-timeout timer. Only real data — `stt`, `llm`, or `tts` frames —
resets it. So the keepalive keeps the *transport* alive without disabling the
*application*-level timeout that lets the Tab5 give up on a genuinely wedged
turn. The two timers serve different jobs and are intentionally decoupled.

### Auto-fallback: degrade, never dead-air

Cloud and gateway backends can fail mid-turn. The pipeline never leaves the user
in silence; it degrades and tells the Tab5 what happened.

- **Cloud STT/TTS fails (timeout or API error):** the pipeline falls back to
  local Moonshine/Piper for *that request*, then sends a `config_update` carrying
  an `error` field, which pushes the Tab5 back to Local mode for subsequent
  turns. `LLMConfig.local_backend` remembers the original local backend so the
  swap target is always known.
- **TinkerClaw gateway down (connection refused on `localhost:18789`):** Dragon
  sends an `error` to the Tab5 — the same auto-revert-to-Local pattern.
- **`lmstudio` socket unreachable at process start:** `create_llm()`
  transparently falls back to Ollama for the whole session. This one is
  **one-shot at backend creation, not per-request**, so warm-path latency is
  unaffected — but it also means restarting `tinkerclaw-llama-server`
  mid-session is not picked up until you `systemctl restart tinkerclaw-voice`.

The full fallback table is in the [voice modes reference](../reference/voice-modes.md#auto-fallback).

### Audio rates: why two re-rate steps bracket the flow

The whole pipeline standardizes on **16 kHz mono int16 PCM on the wire**, but
neither the microphone nor the TTS engines run at 16 kHz natively. So there are
re-rate steps at both ends:

| Where | Conversion | Done by |
|---|---|---|
| Tab5 mic capture | 48 kHz → 16 kHz (3:1 box filter, average of 3 samples) | Tab5 firmware |
| Piper TTS output | 22 050 Hz → 16 kHz (linear interpolation) | Dragon `pipeline.py` |
| OpenRouter TTS output | 24 kHz → 16 kHz | Dragon `pipeline.py` |
| Tab5 speaker playback | 16 kHz → 48 kHz (linear interpolation) | Tab5 firmware |

The 16 kHz wire rate is chosen because it is the rate the STT models expect and
it halves the bytes on the wire versus 24 kHz, which matters for a battery client
over WiFi. The Dragon always resamples *down* to 16 kHz before sending; the Tab5
always upsamples *up* to its 48 kHz codec rate for playback. Sending TTS at its
native 22 050/24 kHz rate would force the Tab5 to resample a rate it does not
expect and would inflate the wire payload — so the Dragon eats the resample cost
once, server-side, where there is CPU to spare.

## Why it's built this way

**One pipeline object, three turn shapes.** Ask, dictate, and text turns are not
separate code paths bolted together; they are configurations of one
`VoicePipeline`. Dictate is "stop after STT, post-process later"; text is "skip
STT"; ask is the full flow. Keeping them in one object means VAD, cancellation,
session bookkeeping, and the keepalive are written once and shared. The cost is
that `pipeline.py` carries conditionals for the mode — an acceptable trade for
not maintaining three near-identical orchestrators.

**Sentence buffering over whole-reply synthesis.** Synthesizing the full reply
before speaking would be simpler and would let the TTS engine make better
prosody decisions across the whole utterance. We pay that small quality cost to
buy seconds of perceived latency, because on a voice assistant the gap between
"stop talking" and "start hearing a reply" is the experience. A user forgives a
slightly clipped sentence boundary; they do not forgive ten seconds of silence.

**Keepalive instead of a longer client timeout.** We could have told the Tab5 to
wait longer before reconnecting. We did not, because a long fixed timeout makes
*genuinely* dead connections take a long time to recover, and it does nothing for
a turn that exceeds the timeout anyway. Pinging every 5 s keeps the transport
alive for arbitrarily long turns while leaving the Tab5's *data*-driven response
timeout free to fire on a real hang. The keepalive and the response timeout are
deliberately on different clocks — see the protocol note above.

**Fallback that reverts the mode, not just the request.** When cloud STT fails,
the per-request local fallback alone would let the next turn fail the same way.
Pushing the Tab5 back to Local mode means the failure is sticky until the user
re-opts into cloud — the system assumes a cloud outage persists rather than
optimistically retrying every turn. That is the safe default for a thing held in
your hand: degrade once, stay degraded, let the human decide when to climb back.

**16 kHz on the wire, resample at the edges.** The alternative — carrying each
engine's native rate end-to-end — would push resampling onto the battery-powered
Tab5 and vary the wire format by backend. Standardizing the wire on 16 kHz makes
the Tab5's audio path identical regardless of which TTS engine the Dragon used,
and concentrates the resampling cost on the side with a real CPU. The product
constraint ("Tab5 + Dragon, nothing else") makes the Dragon the right place to
spend cycles.

**Pacing at 80% real-time.** Streaming audio faster than the speaker consumes it
is the classic way to overrun a small embedded ring buffer. Pacing slightly under
real-time keeps the buffer fed without overflowing, and front-loading the first
few chunks hides the pacing from the listener at the start of playback. It is a
flow-control decision dressed as an audio decision.

## See also

- [Voice modes reference](../reference/voice-modes.md) · [WebSocket protocol reference](../reference/websocket-protocol.md) · [Tools catalog](../reference/tools-catalog.md)
- [Swap the LLM backend](../how-to/swap-the-llm-backend.md) · [Configure the multi-model router](../how-to/configure-the-multi-model-router.md)
- [`docs/protocol.md`](../protocol.md) — the canonical full wire specification · [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md) — the system map
