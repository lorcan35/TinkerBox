"""Tab5 `stop` WS command handler.

Wave 23 SOLID-audit follow-up — thirteenth sub-extract from
the WS-handler family in server.py (round 4 spillover, after
the twelve prior extracts #227-#238).

When Tab5 sends a `stop` frame (mic released after PTT or
dictation finished), the server has to:

  1. **Dictate mode** — call `pipeline.finish_dictation()` which
     runs STT + post-processing.  When the transcript is
     non-trivial (> MIN_DICTATION_CHARS), auto-save it as a Dragon note via
     `notes_svc.create_from_text()` and emit a `note_created`
     frame so Tab5 can show the freshly-saved note.
  2. **Ask / other modes** — call `pipeline.start_processing()`
     which runs the STT → LLM → TTS chain on the buffered audio.

Both branches run inside the per-connection `conn_lock`
(US-P10) so they serialise against text-cmd processing on the
same connection.

Pre-extract this 30-LOC handler lived inline in
`_handle_ws_voice`'s cmd_type dispatch.  Now lives here so
the dispatch table stays a flat one-liner per command.

## API

```python
await handle_stop_command(
    ws,
    *,
    ws_id,
    conn_state,
    conn_lock,
    notes_svc,
) -> None
```

`conn_lock` is the per-connection `asyncio.Lock` (US-P10).
`notes_svc` is the NotesService singleton — when missing or
when the dictation transcript is too short to be useful
(≤ MIN_DICTATION_CHARS), the note auto-create silently skips.

## Why the MIN_DICTATION_CHARS transcript gate

Dictation post-processing on a near-empty buffer can produce
filler like " " or "Uh." or " uhh." after Whisper's silence
trim.  Persisting these as notes would clutter the notes view
without value.  The MIN_DICTATION_CHARS floor is a proxy for
"the user actually said something dictatable" and is shared
with the pipeline summary gate so the two never disagree.

## Why try/except around create_from_text

Note auto-create failure (DB hiccup, embedder timeout) MUST
NOT fail the stop command — the dictation transcript already
landed via `finish_dictation`'s `dictation_summary` frame, and
the note is a "while we're here, also save it" convenience.
Logged at ERROR for ops visibility but never re-raised.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from aiohttp import web

from dragon_voice.dictation_post import MIN_DICTATION_CHARS

logger = logging.getLogger(__name__)


# The note-auto-save gate shares MIN_DICTATION_CHARS with the
# pipeline's summary gate (dictation audit S3-3) — a transcript that is
# too short to summarize is also too short to save as a note, so the two
# can't disagree (pre-fix an 11-20 char transcript saved a note while
# the pipeline reported it EMPTY).


async def handle_stop_command(
    ws: web.WebSocketResponse,
    *,
    ws_id: str,
    conn_state: dict,
    conn_lock: asyncio.Lock,
    notes_svc: Optional[Any],   # NotesService (Optional in test paths)
) -> None:
    """Handle a Tab5 `stop` WS frame.

    No-op when the pipeline isn't attached yet (boot race).

    Mode dispatch:
      * ``dictate`` → `pipeline.finish_dictation()` + auto-note
        when transcript is > MIN_DICTATION_CHARS and notes_svc is available.
      * ``ask`` (or anything else) → `pipeline.start_processing()`
        — runs the STT → LLM → TTS chain on the buffered audio.

    Both branches run inside ``conn_lock`` so they serialise
    against any concurrent text-cmd handler on the same
    connection (US-P10).
    """
    pipeline = conn_state.get("pipeline")
    if not pipeline:
        return

    mode = conn_state.get("mode", "ask")
    buf_size = (
        len(pipeline._audio_buffer)
        + len(pipeline._segment_buffer)
    )
    logger.info(
        "Connection %s: stop (mode=%s, buffer=%d bytes)",
        ws_id, mode, buf_size,
    )

    async with conn_lock:  # US-P10: serialise with text
        if mode == "dictate":
            transcript = await pipeline.finish_dictation()
            await _maybe_auto_create_note(
                ws,
                transcript=transcript,
                notes_svc=notes_svc,
                turn_id=conn_state.get("turn_id", "-"),
            )
        else:
            await pipeline.start_processing()


async def _maybe_auto_create_note(
    ws: web.WebSocketResponse,
    *,
    transcript: Optional[str],
    notes_svc: Optional[Any],
    turn_id: str = "-",
) -> None:
    """Auto-save the dictation transcript as a Dragon note +
    emit `note_created`.  Silently skips on:

      * No transcript / whitespace-only.
      * Transcript too short (≤10 chars — filler from silence
        trim).
      * No notes service available (test path / boot race).

    Failure to create the note is caught + logged at ERROR but
    never re-raised — the transcript already landed via the
    pipeline's `dictation_summary` frame; the note is a
    "while we're here, also save it" convenience.
    """
    if not transcript:
        return
    stripped = transcript.strip()
    if len(stripped) <= MIN_DICTATION_CHARS:
        return
    if notes_svc is None:
        return

    try:
        note = await notes_svc.create_from_text(stripped, title="")
        logger.info(
            "Auto-created note %s from dictation (%d chars)",
            note.id, len(transcript),
        )
        if not ws.closed:
            await ws.send_json({
                "type": "note_created",
                "note_id": note.id,
                "title": note.title,
                "transcript": transcript[:200],
                # W2: stamp the turn_id (note_created bypasses the pipeline
                # callback's auto-stamp) so W4 can dedup the note by turn_id.
                "turn_id": turn_id,
            })
    except Exception as e:
        logger.error("Failed to auto-create dictation note: %s", e)
