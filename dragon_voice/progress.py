"""Unified progress event bus (β-arch).

Phase 6 of the UX-gap remediation (see docs/UX-GAPS.md / issue #123).

Phases 1-4 shipped point-fixes for the streaming-feedback gaps
(H1 / H2 / H4) — each introduced its own bespoke event vocabulary
on the Dragon → Tab5 WebSocket protocol:

  * H4 dictation post-process → ``dictation_postprocessing`` /
    ``_error`` / ``_cancelled`` / ``dictation_summary``
  * H1 tool-call streaming   → ``tool_call`` / ``tool_result``
  * H2 TTS chunk-flushing    → ``tts_start`` / ``tts_end``

Every new progress signal we want to add (RAG retrieval phase,
embedding batch progress, scheduler ticks, …) needs a fresh switch
case in Tab5's voice.c and a fresh server emit-site convention.
The audit's β-arch row calls for collapsing these into a single
uniform ``progress`` channel so:

  * Tab5 has ONE renderer instead of N
  * Future progress signals plug in for free without protocol churn
  * Server-side observability gets a single event class to log/meter

This module is the Dragon-side builder.  It mirrors
``dragon_voice/errors.py`` exactly: pure functions + enums, no
I/O, no state — emission is the caller's job (typically via
``dragon_voice/progress_emit.py``'s pair helper).

Wire format:

.. code:: json

    {
      "type": "progress",
      "phase": "dictation_post" | "tool" | "tts" | "stt" | "llm" | "media_render",
      "stage": "start" | "update" | "done" | "error" | "cancelled",
      "payload": { /* phase-specific dict, optional */ },
      "code": "...",       // present when stage in {error, cancelled}
      "message": "...",    // present when stage in {error, cancelled}
      "severity": "transient" | "fatal",  // present on error stage
      "scope": "..."       // present on error stage
    }

Phase / Scope distinction (this trips people up):

  * **Phase** = where the work is happening (lifecycle).  STT, LLM,
    TTS, TOOL, DICTATION_POST, MEDIA_RENDER.
  * **Scope** = where an *error* originated (taxonomy from γ1's
    errors.py).  STT, LLM, TTS, TOOL, SESSION, DEVICE, GATEWAY,
    MEDIA, UNKNOWN.

They overlap but aren't the same set: ``MEDIA_RENDER`` is a Phase
but not a Scope (no errors emit from rendering itself, only from
the LLM/TOOL phases that produce the content); ``DEVICE`` and
``SESSION`` are Scopes but not Phases (no progress lifecycle for
"device-claimed-the-session" — it's purely an error condition).

Backward compatibility:
  Migrated emitters double-write — they send BOTH the legacy event
  AND this new ``progress`` event.  Tab5 firmware doesn't have to
  update immediately (verified: an unmodified Tab5 silently ignores
  unknown ``type`` values via voice.c's if/else-if chain with no
  terminal panic).  Once Tab5 ships a ``progress``-aware build,
  ``VoiceConfig.progress_bus_emit_legacy = False`` drops the legacy
  emit lines.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from dragon_voice.errors import Scope, Severity


class Phase(str, Enum):
    """Lifecycle phase of work the progress event describes.

    The string values are the on-the-wire representation in the JSON
    frame's ``phase`` field, kept short for byte-budget on Tab5's
    cJSON parser.
    """

    STT = "stt"
    """Speech-to-text transcription pipeline phase."""

    LLM = "llm"
    """LLM token-generation phase (streamed and full-response)."""

    TTS = "tts"
    """Text-to-speech audio synthesis phase."""

    TOOL = "tool"
    """Tool-calling phase — invocation, execution, result return,
    or args-parse failure."""

    DICTATION_POST = "dictation_post"
    """Post-dictation summary generation phase (LLM produces a
    title + summary from a multi-minute transcript)."""

    MEDIA_RENDER = "media_render"
    """Rich-media rendering phase (Pygments code blocks, Pillow
    tables, image-URL fetches)."""


class Stage(str, Enum):
    """Lifecycle stage within a phase.

    A long-running phase (LLM, dictation_post, RAG retrieval) emits
    ``start`` once, ``update`` zero or more times, then exactly one
    of ``done`` / ``error`` / ``cancelled``.  A cheap atomic phase
    (a single tool call) may emit only ``start`` then ``done``,
    skipping update.
    """

    START = "start"
    """Work begins.  Tab5 should put the relevant UI surface into
    a "working" state (spinner, progress indicator, etc.)."""

    UPDATE = "update"
    """Work continues with new partial information.  Tab5 should
    refresh the in-progress UI surface — does not change overall
    state."""

    DONE = "done"
    """Work completed successfully.  Tab5 should clear the
    "working" state and present the result from ``payload``."""

    ERROR = "error"
    """Work failed.  Carries the γ1 error taxonomy (``code``,
    ``message``, ``severity``, ``scope``) so Tab5's existing
    routing-by-severity logic (γ2-H8) applies."""

    CANCELLED = "cancelled"
    """Work was cancelled (typically because a newer request
    superseded it).  Carries ``code`` + ``message`` like ``error``
    but is semantically distinct — no operator action needed."""


def progress_event(
    *,
    phase: Phase,
    stage: Stage,
    payload: Optional[dict] = None,
    code: Optional[str] = None,
    message: Optional[str] = None,
    severity: Optional[Severity] = None,
    scope: Optional[Scope] = None,
) -> dict:
    """Build a structured progress event dict for WS emission.

    Pure function — caller is responsible for the actual
    ``ws.send_json`` / ``await self._on_event(...)`` call so this
    helper stays unit-testable without mocking I/O.  Use
    :func:`dragon_voice.progress_emit.emit_progress_pair` for the
    typical "send legacy + send progress" double-write path.

    Parameters
    ----------
    phase:
        Which lifecycle phase the event belongs to (DICTATION_POST,
        TOOL, etc.).
    stage:
        Where in the phase lifecycle this event is (START, UPDATE,
        DONE, ERROR, CANCELLED).
    payload:
        Phase-specific dict (e.g. ``{"title": "...", "summary":
        "..."}`` for dictation_post.done; ``{"tool": "...", "args":
        {...}}`` for tool.start).  Omitted on minimal events.
    code:
        Required when ``stage`` in (ERROR, CANCELLED).  Snake-case
        machine-readable identifier (e.g. ``"no_llm_available"``,
        ``"dictation_post_cancelled"``).
    message:
        Required when ``stage`` in (ERROR, CANCELLED).  User-facing
        string — short, actionable, no implementation detail.
    severity:
        For ``stage = ERROR`` only.  Defaults to TRANSIENT (the
        same default as :func:`dragon_voice.errors.error_event`).
    scope:
        For ``stage = ERROR`` only.  Defaults to UNKNOWN.

    Returns
    -------
    dict
        Ready-to-send JSON dict.

    Raises
    ------
    ValueError
        If ``stage`` is ERROR or CANCELLED but ``code`` / ``message``
        are missing.  Better to fail loud at the emit site than
        ship a malformed frame to Tab5.
    """
    if stage in (Stage.ERROR, Stage.CANCELLED):
        if not code or not message:
            raise ValueError(
                f"progress_event(stage={stage.value!r}) requires both "
                f"`code` and `message` — got code={code!r}, message={message!r}"
            )

    frame: dict = {
        "type": "progress",
        "phase": phase.value,
        "stage": stage.value,
    }
    if payload is not None:
        frame["payload"] = payload
    if code is not None:
        frame["code"] = code
    if message is not None:
        frame["message"] = message
    # Severity / scope are meaningful only on ERROR stage; carry
    # them through so Tab5's γ2-H8 router can apply.  Default to
    # TRANSIENT/UNKNOWN matching errors.py for safety.
    if stage == Stage.ERROR:
        frame["severity"] = (severity or Severity.TRANSIENT).value
        frame["scope"] = (scope or Scope.UNKNOWN).value
    return frame


__all__ = ["Phase", "Stage", "progress_event"]
