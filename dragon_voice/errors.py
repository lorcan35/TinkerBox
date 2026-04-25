"""Structured error taxonomy for Dragon → Tab5 error frames.

Phase 3 γ1 of the UX-gap remediation (see docs/UX-GAPS.md / issue #101).

Pre-fix the codebase emitted ad-hoc `{"type":"error","message":"raw_string"}`
frames from ~11 different sites with no shared schema.  Tab5
(voice.c:754-764) routed every error to the voice-state caption buffer
regardless of severity — implementation-detail strings like
``[Ollama timeout after 300s]`` or raw `str(e)` Python exception text
appeared in the voice overlay caption with no user-actionable context.

This module introduces:

* :class:`Severity` enum — TRANSIENT (retry-able, e.g. STT miss, LLM
  timeout) vs FATAL (unrecoverable, e.g. session_invalid, auth_failed,
  device_evicted).  Tab5 will use this in γ2 to route TRANSIENT errors
  to a non-blocking toast and FATAL errors to the voice caption +
  retry banner.
* :class:`Scope` enum — which subsystem produced the error (STT, LLM,
  TTS, TOOL, SESSION, DEVICE, GATEWAY, MEDIA).  Used by Tab5 to choose
  an appropriate icon/colour for the toast, and by Dragon-side
  observability to bucket errors in metrics / logs.
* :func:`error_event` — the structured-frame builder.  All ad-hoc
  emission sites are migrated to call this so the schema stays
  consistent.
* :class:`DragonError` exception — for raise/catch flows in backends
  and tools that want to signal a structured error upward.  Carries
  the same severity + scope + user-friendly message + machine code.

Backward compatibility: the emitted frame still has the existing
``type``, ``code``, and ``message`` fields — so an unmodified Tab5
that reads only ``message`` keeps working.  The new ``severity`` and
``scope`` fields are additive; Tab5 starts honouring them in γ2.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional


class Severity(str, Enum):
    """Error severity — drives Tab5's UI surface choice (toast vs
    caption vs retry banner).

    The string values are the on-the-wire representation in the JSON
    frame's ``severity`` field, kept short for byte-budget on Tab5's
    cJSON parser.
    """

    TRANSIENT = "transient"
    """Retry is meaningful — e.g. STT didn't hear anything, LLM took too
    long, network flap.  Tab5 should surface as a non-blocking toast
    and stay in READY so the user can try again immediately."""

    FATAL = "fatal"
    """Retry won't help without operator action — e.g. session_invalid,
    auth_failed, device_evicted, no LLM configured.  Tab5 should
    surface in the voice caption (more permanent) and may stop
    auto-reconnecting if appropriate."""


class Scope(str, Enum):
    """Which subsystem produced the error.

    Used by Tab5 for icon/colour selection in γ2 and by Dragon-side
    observability to bucket errors in metrics.
    """

    STT = "stt"
    LLM = "llm"
    TTS = "tts"
    TOOL = "tool"
    SESSION = "session"
    DEVICE = "device"
    GATEWAY = "gateway"
    MEDIA = "media"
    UNKNOWN = "unknown"


def error_event(
    *,
    code: str,
    message: str,
    severity: Severity = Severity.TRANSIENT,
    scope: Scope = Scope.UNKNOWN,
) -> dict:
    """Build a structured error event dict for WS emission.

    Replaces ad-hoc ``{"type":"error","message":"..."}`` frames so
    every emission site contributes the same schema.  Tab5 (γ2) uses
    ``severity`` to route to the right UI surface; ``scope`` informs
    icon/colour; ``code`` is the machine-readable identifier; and
    ``message`` is the user-facing string (kept short — Tab5's caption
    buffer is 128 chars).

    Parameters
    ----------
    code:
        Machine-readable identifier (e.g. ``"stt_empty"``, ``"llm_timeout"``,
        ``"session_invalid"``).  Snake-case.  Stable enough for Tab5
        to switch on; not stable enough to be considered a public API.
    message:
        User-facing message.  Short, actionable, no implementation
        detail.  Don't pass ``str(e)`` — that leaks Python exception
        types.  Bad: ``"list index out of range"``.  Good:
        ``"Image analysis failed — please try again"``.
    severity:
        TRANSIENT (retry-able) vs FATAL (needs operator action).
    scope:
        Which subsystem (LLM, TTS, etc.).  UNKNOWN is acceptable when
        the source is genuinely ambiguous.

    Returns
    -------
    dict
        Ready-to-send JSON dict.  Caller is responsible for the actual
        ``ws.send_json`` / ``await self._on_event(...)`` call so this
        helper stays a pure function (testable, no I/O).
    """
    return {
        "type": "error",
        "code": code,
        "message": message,
        "severity": severity.value,
        "scope": scope.value,
    }


class DragonError(Exception):
    """Exception form of a structured error.

    Useful in backends (LLM, STT, TTS) and tools that want to ``raise``
    a typed error upward instead of returning a status dict.  The
    catching layer can convert to an emit via :meth:`to_event`.

    Example::

        if not gateway_reachable:
            raise DragonError(
                "TinkerClaw gateway is offline",
                code="gateway_unreachable",
                severity=Severity.FATAL,
                scope=Scope.GATEWAY,
            )

        try:
            ...
        except DragonError as e:
            await self._on_event(e.to_event())
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        severity: Severity = Severity.TRANSIENT,
        scope: Scope = Scope.UNKNOWN,
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.severity = severity
        self.scope = scope
        self.cause = cause

    def to_event(self) -> dict:
        """Convert to the standard error-event dict for WS emission."""
        return error_event(
            code=self.code,
            message=self.message,
            severity=self.severity,
            scope=self.scope,
        )

    def __repr__(self) -> str:
        return (
            f"DragonError(code={self.code!r}, severity={self.severity.value!r}, "
            f"scope={self.scope.value!r}, message={self.message!r})"
        )


__all__ = ["Severity", "Scope", "error_event", "DragonError"]
