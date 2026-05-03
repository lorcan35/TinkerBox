"""Out-of-band system-message speech for `VoicePipeline`.

Wave 23 SOLID-audit follow-up — thirty-ninth sub-extract.
Sixth slice from `dragon_voice/pipeline.py` (audit SRP-4).

`speak_system` lets the server speak a short alert (e.g.
budget auto-downgrade — Gauntlet G7-F) out-of-band of the
normal STT → LLM → TTS turn loop.  The user hears the
message via the live TTS path so it respects the currently-
selected voice (Piper / OpenRouter) and inherits the
existing pacing + resampling.

Pre-extract this 30 LOC was a method on `VoicePipeline` that
wrapped `_synthesize_and_send` with a `tts_start`-tracking
bracket so the post-utterance `tts_end` event fires
correctly.  Now lives as a free function taking the pipeline
explicitly.

## API

```python
await speak_system_message(pipeline, "Switching to Local mode.")
```

The function is a thin functional wrapper around pipeline
state — it reads `pipeline._tts`, `pipeline._tts_started`,
`pipeline._tts_total_ms`, `pipeline._on_event` and calls
`pipeline._synthesize_and_send`.  Tests can mock the pipeline
attribute surface.

## Bracket invariant (preserved verbatim)

  1. Snapshot `_tts_started` BEFORE the synth.
  2. Run `_synthesize_and_send(text)` (which may flip
     `_tts_started` from False → True via its own `tts_start`
     emit on first sentence).
  3. After synth (success or failure), if WE were the one
     who flipped it (`_tts_started AND not prev_started`),
     emit `tts_end` so Tab5 flushes its ring buffer + clear
     the flag.

The `not prev_started` check prevents double-`tts_end` when
speak_system is called mid-utterance (the active utterance's
caller owns the close).

## Failure isolation

* Synth failure logged at EXCEPTION but NEVER re-raised —
  callers (e.g. cap_downgrade `speak_system_message`) are
  fire-and-forget and shouldn't crash on TTS failure.
* `tts_end` emit failure swallowed (best-effort — the
  user's already heard the audio; the close-frame is just
  for ring-buffer hygiene).
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def speak_system_message(pipeline: Any, text: str) -> None:
    """Speak `text` via the pipeline's live TTS path,
    out-of-band of the conversation turn loop.

    No-op when:
      * `text` is empty / falsy
      * `pipeline._tts` is None (TTS not initialised yet)

    Side effects (via the pipeline instance):
      * Calls `pipeline._synthesize_and_send(text)` which may
        flip `pipeline._tts_started` from False to True and
        accumulate `pipeline._tts_total_ms`.
      * Emits `tts_end` via `pipeline._on_event` IFF this
        invocation was the one that started the utterance.

    Failures swallowed: this is a fire-and-forget path used
    by ops alerts (cap downgrade, etc.); a TTS error must NOT
    propagate up into the caller's coroutine.
    """
    if not text or not pipeline._tts:
        return
    prev_started = pipeline._tts_started
    try:
        await pipeline._synthesize_and_send(text)
    except Exception:
        logger.exception("speak_system failed: %s", text[:40])
    finally:
        # Close the utterance so Tab5 flushes its ring buffer.
        # Only emit if WE were the one who started it — prevents
        # double-tts_end when speak_system fires mid-utterance.
        if pipeline._tts_started and not prev_started:
            try:
                await pipeline._on_event({
                    "type": "tts_end",
                    "tts_ms": round(pipeline._tts_total_ms),
                })
            except Exception:
                pass
            pipeline._tts_started = False
