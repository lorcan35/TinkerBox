"""Unit tests for the unified progress event bus builder.

β-arch (Phase 6, issue #123, refs #89, #94).

Mirrors ``tests/test_errors.py`` shape — pure-function tests for
the wire-format builder + enum vocabulary.  No mocks, no I/O.
"""
from __future__ import annotations

import json

import pytest

from dragon_voice.errors import Scope, Severity
from dragon_voice.progress import Phase, Stage, progress_event


# ───────────────────────── enum on-the-wire shape


def test_phase_values_are_short_strings() -> None:
    """Tab5's cJSON parser has byte budget — keep wire values terse.
    Pin the exact set so a future addition is a deliberate enum
    extension, not silent drift (matches the γ1 test pattern)."""
    expected = {"stt", "llm", "tts", "tool", "dictation_post", "media_render"}
    assert {p.value for p in Phase} == expected


def test_stage_covers_all_lifecycle_states() -> None:
    """Pin the exact set — start / update / done / error / cancelled
    is the contract Tab5 will switch on once it's progress-aware."""
    expected = {"start", "update", "done", "error", "cancelled"}
    assert {s.value for s in Stage} == expected


# ───────────────────────── progress_event() shape


def test_progress_event_minimal_shape() -> None:
    """Start/update/done with only phase + stage produce a 3-key
    frame: type / phase / stage.  No spurious payload / code /
    message keys."""
    ev = progress_event(phase=Phase.DICTATION_POST, stage=Stage.START)
    assert ev == {
        "type": "progress",
        "phase": "dictation_post",
        "stage": "start",
    }


def test_progress_event_with_payload() -> None:
    """Phase-specific payload dict round-trips intact."""
    ev = progress_event(
        phase=Phase.DICTATION_POST,
        stage=Stage.DONE,
        payload={"title": "Note Title", "summary": "Some summary text."},
    )
    assert ev["payload"] == {"title": "Note Title", "summary": "Some summary text."}
    assert ev["type"] == "progress"
    assert ev["phase"] == "dictation_post"
    assert ev["stage"] == "done"


def test_progress_event_error_stage_includes_taxonomy() -> None:
    """ERROR stage carries the γ1 taxonomy (severity, scope, code,
    message) so Tab5's γ2-H8 routing-by-severity logic applies."""
    ev = progress_event(
        phase=Phase.TOOL,
        stage=Stage.ERROR,
        code="tool_args_invalid",
        message="Tool 'calculator' had invalid arguments — skipped.",
        severity=Severity.TRANSIENT,
        scope=Scope.TOOL,
    )
    assert ev["code"] == "tool_args_invalid"
    assert ev["message"].startswith("Tool 'calculator'")
    assert ev["severity"] == "transient"
    assert ev["scope"] == "tool"
    assert ev["stage"] == "error"
    assert ev["phase"] == "tool"


def test_progress_event_error_defaults_to_transient_unknown() -> None:
    """Caller-omitted severity/scope on ERROR stage defaults to
    TRANSIENT/UNKNOWN — same defaults as errors.py for safety."""
    ev = progress_event(
        phase=Phase.LLM,
        stage=Stage.ERROR,
        code="llm_failed",
        message="Generation failed.",
    )
    assert ev["severity"] == "transient"
    assert ev["scope"] == "unknown"


def test_progress_event_cancelled_stage_carries_code_and_message() -> None:
    """CANCELLED is semantically distinct from ERROR (no operator
    action needed) but still carries code + message — Tab5 needs to
    know WHAT was cancelled, even if it doesn't surface a banner."""
    ev = progress_event(
        phase=Phase.DICTATION_POST,
        stage=Stage.CANCELLED,
        code="dictation_post_cancelled",
        message="Prior summary abandoned for new dictation.",
    )
    assert ev["code"] == "dictation_post_cancelled"
    assert ev["message"] == "Prior summary abandoned for new dictation."
    # CANCELLED does NOT carry severity/scope — those are error-only.
    assert "severity" not in ev
    assert "scope" not in ev


def test_progress_event_error_without_code_raises() -> None:
    """Fail loud at the emit site — better than shipping a
    malformed frame to Tab5."""
    with pytest.raises(ValueError, match="requires both"):
        progress_event(phase=Phase.TOOL, stage=Stage.ERROR, message="just a message")


def test_progress_event_error_without_message_raises() -> None:
    with pytest.raises(ValueError, match="requires both"):
        progress_event(phase=Phase.TOOL, stage=Stage.ERROR, code="x")


def test_progress_event_cancelled_without_code_raises() -> None:
    """CANCELLED has the same code+message requirement as ERROR."""
    with pytest.raises(ValueError, match="requires both"):
        progress_event(phase=Phase.DICTATION_POST, stage=Stage.CANCELLED, message="m")


def test_progress_event_is_pure() -> None:
    """No hidden state, no I/O — callable repeatedly with same args
    returns equal dicts.  Important for unit-testing emission sites."""
    a = progress_event(
        phase=Phase.TOOL,
        stage=Stage.DONE,
        payload={"tool": "datetime", "result": {"now": "2026-04-26"}},
    )
    b = progress_event(
        phase=Phase.TOOL,
        stage=Stage.DONE,
        payload={"tool": "datetime", "result": {"now": "2026-04-26"}},
    )
    assert a == b


# ───────────────────────── frame is JSON-serialisable


def test_progress_event_is_json_serializable() -> None:
    """Caller passes the dict to ws.send_json — must be plain
    JSON-friendly (no enums leaking through)."""
    ev = progress_event(
        phase=Phase.DICTATION_POST,
        stage=Stage.ERROR,
        code="no_llm_available",
        message="No language model is configured.",
        severity=Severity.FATAL,
        scope=Scope.LLM,
    )
    encoded = json.dumps(ev)
    decoded = json.loads(encoded)
    assert decoded["phase"] == "dictation_post"
    assert decoded["stage"] == "error"
    assert decoded["severity"] == "fatal"
    assert decoded["scope"] == "llm"


# ───────────────────────── shape-vs-legacy compatibility


def test_progress_dictation_done_payload_mirrors_legacy_summary_keys() -> None:
    """The old ``dictation_summary`` event carries title + summary
    at the top level.  The new progress.dictation_post.done event
    carries the same keys inside ``payload``.  Pin so a Tab5
    implementation can map between them without surprise."""
    ev = progress_event(
        phase=Phase.DICTATION_POST,
        stage=Stage.DONE,
        payload={"title": "Test Title", "summary": "Body text"},
    )
    # Same field NAMES the legacy `dictation_summary` event uses.
    assert "title" in ev["payload"]
    assert "summary" in ev["payload"]


def test_progress_tool_start_payload_mirrors_legacy_tool_call_keys() -> None:
    """Legacy `tool_call` carries `tool` + `args` at the top level.
    New progress.tool.start carries them inside `payload`."""
    ev = progress_event(
        phase=Phase.TOOL,
        stage=Stage.START,
        payload={"tool": "web_search", "args": {"query": "esp32"}},
    )
    assert "tool" in ev["payload"]
    assert "args" in ev["payload"]
