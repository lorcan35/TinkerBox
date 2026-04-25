"""Unit tests for the structured error taxonomy.

Phase 3 γ1 of the UX-gap remediation (see docs/UX-GAPS.md / issue #101).
"""
from __future__ import annotations

import pytest

from dragon_voice.errors import DragonError, Scope, Severity, error_event


# ───────────────────────── enum on-the-wire shape


def test_severity_values_are_short_strings() -> None:
    """Tab5's cJSON parser has byte budget — keep wire values terse."""
    assert Severity.TRANSIENT.value == "transient"
    assert Severity.FATAL.value == "fatal"
    # No accidental lowercase-hash collisions
    assert {s.value for s in Severity} == {"transient", "fatal"}


def test_scope_covers_all_subsystems_in_audit() -> None:
    """The Phase-2 audit identified these 8 subsystems as error-emitting.
    Pin them down so a future addition is a deliberate enum extension,
    not silent drift."""
    expected = {"stt", "llm", "tts", "tool", "session", "device", "gateway", "media", "unknown"}
    actual = {s.value for s in Scope}
    assert actual == expected


# ───────────────────────── error_event() shape


def test_error_event_has_all_fields() -> None:
    ev = error_event(
        code="llm_timeout",
        message="Thinking took too long — try a shorter question",
        severity=Severity.TRANSIENT,
        scope=Scope.LLM,
    )
    assert ev["type"] == "error"
    assert ev["code"] == "llm_timeout"
    assert ev["message"] == "Thinking took too long — try a shorter question"
    assert ev["severity"] == "transient"
    assert ev["scope"] == "llm"


def test_error_event_defaults_are_safe() -> None:
    """If a caller forgets to specify severity/scope, default to
    TRANSIENT/UNKNOWN — the LEAST disruptive interpretation
    (Tab5 will toast it instead of permanently displaying)."""
    ev = error_event(code="x", message="y")
    assert ev["severity"] == "transient"
    assert ev["scope"] == "unknown"


def test_error_event_is_pure() -> None:
    """No hidden state, no I/O — callable repeatedly with same args
    returns equal dicts (important for unit-testing emission sites)."""
    ev1 = error_event(code="x", message="y", severity=Severity.FATAL, scope=Scope.SESSION)
    ev2 = error_event(code="x", message="y", severity=Severity.FATAL, scope=Scope.SESSION)
    assert ev1 == ev2


# ───────────────────────── DragonError exception class


def test_dragon_error_round_trips_via_to_event() -> None:
    """A raised + caught DragonError can be serialised via to_event()
    and matches what error_event() would have built directly."""
    e = DragonError(
        "TinkerClaw gateway is offline",
        code="gateway_unreachable",
        severity=Severity.FATAL,
        scope=Scope.GATEWAY,
    )
    ev = e.to_event()
    assert ev == error_event(
        code="gateway_unreachable",
        message="TinkerClaw gateway is offline",
        severity=Severity.FATAL,
        scope=Scope.GATEWAY,
    )


def test_dragon_error_is_a_real_exception() -> None:
    """Can be raised + caught + str()-ed without losing the message."""
    with pytest.raises(DragonError) as exc_info:
        raise DragonError("test error", code="t", severity=Severity.TRANSIENT, scope=Scope.STT)
    assert str(exc_info.value) == "test error"
    assert exc_info.value.code == "t"
    assert exc_info.value.severity is Severity.TRANSIENT
    assert exc_info.value.scope is Scope.STT


def test_dragon_error_carries_cause_for_chained_exceptions() -> None:
    """When a backend wraps an underlying exception, the cause should
    be preserved for logging purposes (not user-visible)."""
    underlying = ValueError("bad input")
    e = DragonError(
        "Image analysis failed",
        code="image_decode_failed",
        severity=Severity.TRANSIENT,
        scope=Scope.MEDIA,
        cause=underlying,
    )
    assert e.cause is underlying
    # to_event() must NOT leak the cause string into the user message
    assert "bad input" not in e.to_event()["message"]


def test_dragon_error_repr_is_useful_for_logs() -> None:
    e = DragonError("x", code="y", severity=Severity.FATAL, scope=Scope.LLM)
    r = repr(e)
    assert "DragonError" in r
    assert "code='y'" in r
    assert "fatal" in r
    assert "llm" in r


# ───────────────────────── frame is JSON-serializable


def test_error_event_is_json_serializable() -> None:
    """Callers pass the dict to ws.send_json / asyncio event hooks —
    must be plain JSON-friendly."""
    import json
    ev = error_event(
        code="x",
        message="y",
        severity=Severity.FATAL,
        scope=Scope.DEVICE,
    )
    # Round-trip through json — no enums leaking through
    encoded = json.dumps(ev)
    decoded = json.loads(encoded)
    assert decoded["severity"] == "fatal"
    assert decoded["scope"] == "device"
