"""Dictation post-processing — generate title + summary via LLM.

Wave 23 SOLID-audit follow-up — twenty-ninth sub-extract.
First slice from `dragon_voice/pipeline.py` (audit SRP-4: the
1733-LOC `VoicePipeline` class owns 5+ responsibilities;
post-process is one of them).

After `finish_dictation` lands the full transcript, this module
runs a lightweight title + summary generation pass via the
active LLM and emits a `dictation_summary` progress event so
Tab5 can surface the auto-generated title in the chat bubble.

Pre-extract this 110-LOC chain lived as
`VoicePipeline._post_process_dictation`.  Now lives in its own
module with the LLM resolution + on_event callback +
emit_legacy flag passed in (DIP).

## API

```python
await run_dictation_post_process(
    transcript,
    *,
    llm,
    on_event,
    emit_legacy,
)
```

`llm` is the resolved LLM backend (caller picks via
`pipeline._conversation_engine.llm` first, then
`pipeline._llm` fallback).  When `llm` is None, a
`no_llm_available` progress.error event fires so Tab5 doesn't
hang on the "Generating summary..." caption.

## Phase 2 H4 (#94) progress events preserved

Three event paths, all wrapped in `emit_progress_pair` for
β-arch (#123) double-write (legacy frame for unmodified Tab5
firmware + new progress frame for the unified bus):

  * No LLM available → `dictation_postprocessing_error` /
    progress.error (TRANSIENT/LLM)
  * Success → `dictation_summary` / progress.done with
    title + summary in payload
  * Failure → `dictation_postprocessing_error` /
    progress.error (TRANSIENT/LLM, code = exception class name)

The "still working" + "cancelled" events are NOT emitted from
here — they fire from `finish_dictation` BEFORE this function
runs (so cancellation of an in-flight prior post-process is
visible immediately).

## Cancellation propagation

`asyncio.CancelledError` is re-raised so the task transitions
to CANCELLED state.  The caller's `add_done_callback` handles
the lifecycle.  The cancelled-side event is NOT emitted from
here — `finish_dictation` already emitted it before spawning
this task.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from dragon_voice.dictation_classifier import classify_dictation
from dragon_voice.errors import Scope, Severity
from dragon_voice.progress import Phase, Stage
from dragon_voice.progress_emit import emit_progress_pair

logger = logging.getLogger(__name__)


def _classify_safe(transcript: str) -> dict:
    """Run the heuristic classifier with a wall-clock guard + fail-soft.

    The classifier is meant to be ~10 ms; if it ever wanders into the
    hundred-millisecond range we want to know AND we want to keep
    dictation_summary flowing.  Returns the empty result on any error.
    """
    started = time.monotonic()
    try:
        # Local TZ for "Tuesday at 6 PM"-style parsing.  Dragon runs in
        # the user's local zone; pull it from /etc/timezone-ish.
        try:
            tz_name = time.tzname[time.daylight] if time.daylight else time.tzname[0]
        except Exception:
            tz_name = "UTC"
        result = classify_dictation(transcript, tz_name=tz_name)
    except Exception:
        logger.exception("classify_dictation crashed — skipping proposed_action")
        return {"kind": "none", "confidence": 0.0, "payload": {}}
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if elapsed_ms > 50:
        logger.warning("classify_dictation took %d ms (target <10 ms)", elapsed_ms)
    return result


OnEvent = Callable[[dict], Awaitable[None]]


# Title + summary prompt template.  Module-level so the contract
# is greppable; tweaks to the wording propagate to a single
# place.  Transcript truncated to 2000 chars to keep the LLM
# context tight on small models.
_PROMPT_TEMPLATE = (
    "Given this voice transcript, provide:\n"
    "1. A short title (max 8 words)\n"
    "2. A 1-2 sentence summary\n\n"
    "Transcript: {transcript}\n\n"
    "Respond in this exact format:\n"
    "TITLE: <title>\nSUMMARY: <summary>"
)
_TRANSCRIPT_PROMPT_LIMIT = 2000

_SYSTEM_PROMPT = "You are a concise note summarizer."

# Defaults when the LLM response doesn't parse cleanly — keeps
# the chat bubble from going blank on a malformed reply.
_DEFAULT_TITLE = "Untitled Note"
_DEFAULT_SUMMARY_TRUNCATE = 200


# PR 2 polish (long-form cross-network resilience):
# When the resolved LLM is the local Ollama backend, skip the LLM call
# and synthesize the title + summary directly from the transcript.
# Ollama on Dragon's CPU takes 60-90 s for the 'TITLE: ... SUMMARY: ...'
# prompt — long enough that ngrok closes the idle WS tunnel before the
# response is ready, leaving Tab5 stuck at TRANSCRIBING forever.
# The auto-created note's first-line-as-title is good enough for Local
# mode; Solo / Cloud modes still go through the LLM (OpenRouter is fast).
def _synthesize_local_title_summary(transcript: str) -> tuple[str, str]:
    transcript = (transcript or '').strip() or 'Untitled'
    words = transcript.split()
    title = ''
    for w in words:
        if len(title) + len(w) + 1 > 50:
            break
        nxt = (title + ' ' + w).strip()
        title = nxt
        if title.count(' ') >= 7:
            break
    if not title:
        title = transcript[:50].strip()
    # Capitalize first letter for note title polish.
    if title and not title[0].isupper():
        title = title[0].upper() + title[1:]
    summary = transcript[:200].strip()
    if len(transcript) > 200:
        summary = summary.rstrip() + '…'
    return title, summary


async def run_dictation_post_process(
    transcript: str,
    *,
    llm: Optional[Any],          # resolved LLM backend (or None)
    on_event: OnEvent,
    emit_legacy: bool,
) -> None:
    """Run the title + summary pass over a completed dictation
    transcript.  Emits progress events for the no-LLM, success,
    and failure paths.

    Args:
        transcript: Full dictation transcript to summarise.
        llm: Resolved LLM backend (caller picks ConvEngine.llm
            first, then pipeline._llm fallback).  None means
            no LLM is available — emit error event + return.
        on_event: Callable that forwards events to the WS layer.
        emit_legacy: Phase 2 H4 (#94) β-arch flag — when True,
            also emit the legacy non-progress frames for
            unmodified Tab5 firmware compatibility.

    Returns: None.  All output flows through on_event.

    Raises: re-raises asyncio.CancelledError so the task
        transitions to CANCELLED state.  Other exceptions are
        caught + emitted as progress.error frames.
    """
    if not llm:
        logger.warning("No LLM available for dictation post-processing")
        # Phase 2 H4 (#94): tell Tab5 the post-process won't run.
        # Pre-fix this would silently log and leave Tab5 waiting
        # for a `dictation_summary` event that never arrives — UI
        # gets stuck on the "Generating summary..." caption forever.
        await emit_progress_pair(
            on_event,
            legacy={
                "type": "dictation_postprocessing_error",
                "error": "no_llm_available",
                "message": "Note saved — summary unavailable (LLM offline)",
            },
            phase=Phase.DICTATION_POST,
            stage=Stage.ERROR,
            code="no_llm_available",
            message="Note saved — summary unavailable (LLM offline)",
            severity=Severity.TRANSIENT,
            scope=Scope.LLM,
            emit_legacy=emit_legacy,
        )
        return


    # PR 2 polish: short-circuit for Local mode (OllamaBackend).
    # Synthesize title/summary from the transcript instead of calling
    # the slow CPU LLM — fires dictation_summary immediately so the
    # pipeline reaches SAVED before the ngrok WS idle-close window.
    backend_name = type(llm).__name__
    if backend_name == 'OllamaBackend':
        title, summary = _synthesize_local_title_summary(transcript)
        # PR 4: heuristic classifier → optional proposed_action chip.
        proposed = _classify_safe(transcript)
        logger.info(
            'Dictation summary (Local-mode synthesized): title=%r summary_len=%d kind=%s conf=%.2f',
            title, len(summary), proposed['kind'], proposed['confidence'],
        )
        legacy_frame: dict[str, Any] = {
            'type': 'dictation_summary',
            'title': title,
            'summary': summary,
        }
        if proposed['kind'] != 'none':
            legacy_frame['proposed_action'] = proposed
        await emit_progress_pair(
            on_event,
            legacy=legacy_frame,
            phase=Phase.DICTATION_POST,
            stage=Stage.DONE,
            payload={'title': title, 'summary': summary, 'proposed_action': proposed},
            emit_legacy=emit_legacy,
        )
        return

    prompt = _PROMPT_TEMPLATE.format(
        transcript=transcript[:_TRANSCRIPT_PROMPT_LIMIT],
    )

    try:
        response = ""
        async for token in llm.generate_stream(prompt, _SYSTEM_PROMPT):
            response += token

        title, summary = _parse_title_summary(response, transcript)

        # PR 4: heuristic classifier → optional proposed_action chip.
        proposed = _classify_safe(transcript)
        logger.info(
            "Dictation summary: title='%s' kind=%s conf=%.2f",
            title, proposed['kind'], proposed['confidence'],
        )
        legacy_frame: dict[str, Any] = {
            "type": "dictation_summary",
            "title": title,
            "summary": summary,
        }
        if proposed['kind'] != 'none':
            legacy_frame['proposed_action'] = proposed
        # β-arch (#123): legacy `dictation_summary` carries
        # title/summary at the top level; the new progress
        # frame nests them in `payload` so the bus is uniform.
        await emit_progress_pair(
            on_event,
            legacy=legacy_frame,
            phase=Phase.DICTATION_POST,
            stage=Stage.DONE,
            payload={"title": title, "summary": summary, "proposed_action": proposed},
            emit_legacy=emit_legacy,
        )
    except asyncio.CancelledError:
        # Cancelled-side event is emitted by `finish_dictation`
        # BEFORE this task spawns — we don't double-emit here.
        # Just propagate so the task transitions to CANCELLED.
        raise
    except Exception as e:
        logger.exception("Dictation post-processing failed")
        # Phase 2 H4 (#94): user-visible error so Tab5 can clear
        # the "Generating summary..." caption + show a toast.
        # The transcript is already in the chat from the prior
        # `stt` event so the user hasn't lost data — they just
        # don't get the auto-generated title/summary.
        await emit_progress_pair(
            on_event,
            legacy={
                "type": "dictation_postprocessing_error",
                "error": type(e).__name__,
                "message": "Note saved — summary generation failed",
            },
            phase=Phase.DICTATION_POST,
            stage=Stage.ERROR,
            code=type(e).__name__,
            message="Note saved — summary generation failed",
            severity=Severity.TRANSIENT,
            scope=Scope.LLM,
            emit_legacy=emit_legacy,
        )


def _parse_title_summary(
    response: str,
    transcript: str,
) -> tuple[str, str]:
    """Parse the LLM reply into (title, summary).

    Defaults to "Untitled Note" + first 200 chars of transcript
    when the reply doesn't contain the expected TITLE: /
    SUMMARY: lines — the LLM may have wandered off-format.
    """
    title = _DEFAULT_TITLE
    summary = transcript[:_DEFAULT_SUMMARY_TRUNCATE]
    for line in response.split("\n"):
        line = line.strip()
        if line.upper().startswith("TITLE:"):
            title = line[6:].strip().strip('"')
        elif line.upper().startswith("SUMMARY:"):
            summary = line[8:].strip().strip('"')
    return title, summary
