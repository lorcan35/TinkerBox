"""Unit tests for the ``emit_progress_pair`` helper.

β-arch (Phase 6, issue #123, refs #89, #94).

The helper is the single seam through which migrated emitters
double-write — sends the legacy ad-hoc event AND the new progress
event in that order.  These tests pin the ordering, the
``emit_legacy=False`` cleanup mode, and the (ConnectionError,
RuntimeError) swallow that prevents a torn-down WS from killing
the calling task.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from dragon_voice.errors import Scope, Severity
from dragon_voice.progress import Phase, Stage
from dragon_voice.progress_emit import emit_progress_pair


def _capturing_on_event() -> tuple[Any, list[dict]]:
    """Return ``(callback, captured_list)``: the callback appends
    every dict it receives to the list."""
    captured: list[dict] = []

    async def cb(ev: dict) -> None:
        captured.append(ev)

    return cb, captured


def test_emit_pair_sends_legacy_then_progress_in_order() -> None:
    """Headline contract: legacy frame is sent FIRST, then the
    progress frame.  Pin so a future refactor can't swap the order
    (Tab5 unmodified parses by `type` — getting `progress` first
    would be silently ignored, but we want consistent ops logs)."""
    cb, captured = _capturing_on_event()

    asyncio.run(emit_progress_pair(
        cb,
        legacy={"type": "dictation_postprocessing"},
        phase=Phase.DICTATION_POST,
        stage=Stage.START,
    ))

    assert len(captured) == 2
    assert captured[0] == {"type": "dictation_postprocessing"}
    assert captured[1]["type"] == "progress"
    assert captured[1]["phase"] == "dictation_post"
    assert captured[1]["stage"] == "start"


def test_emit_pair_skips_legacy_when_emit_legacy_false() -> None:
    """``progress_bus_emit_legacy=False`` is the post-Tab5-update
    cleanup mode — ONLY the progress frame is sent."""
    cb, captured = _capturing_on_event()

    asyncio.run(emit_progress_pair(
        cb,
        legacy={"type": "dictation_postprocessing"},
        phase=Phase.DICTATION_POST,
        stage=Stage.START,
        emit_legacy=False,
    ))

    assert len(captured) == 1
    assert captured[0]["type"] == "progress"


def test_emit_pair_skips_legacy_when_legacy_is_none() -> None:
    """Some future progress signals have no historical equivalent
    (e.g. a brand new RAG-retrieval phase).  ``legacy=None`` means
    "no legacy form exists" — only the progress frame is sent."""
    cb, captured = _capturing_on_event()

    asyncio.run(emit_progress_pair(
        cb,
        legacy=None,
        phase=Phase.LLM,
        stage=Stage.UPDATE,
        payload={"tokens_so_far": 42},
    ))

    assert len(captured) == 1
    assert captured[0]["type"] == "progress"
    assert captured[0]["payload"] == {"tokens_so_far": 42}


def test_emit_pair_swallows_connection_error_on_legacy_send() -> None:
    """Defensive: if the legacy send raises ConnectionError (WS
    torn down between frames), the progress send must still fire.
    Mirrors the existing pattern at pipeline.py:1007."""
    sent: list[dict] = []

    async def flaky_cb(ev: dict) -> None:
        if ev.get("type") == "dictation_postprocessing":
            raise ConnectionError("simulated WS drop on legacy")
        sent.append(ev)

    # MUST NOT raise out — the helper swallows.
    asyncio.run(emit_progress_pair(
        flaky_cb,
        legacy={"type": "dictation_postprocessing"},
        phase=Phase.DICTATION_POST,
        stage=Stage.START,
    ))

    # The progress frame still got through
    assert len(sent) == 1
    assert sent[0]["type"] == "progress"


def test_emit_pair_swallows_connection_error_on_progress_send() -> None:
    """Symmetric — if the progress send fails, we don't bubble it.
    The caller already got at least the legacy frame through (or
    is in legacy-disabled mode), so taking down the loop would be
    needlessly destructive."""
    seen: list[dict] = []

    async def flaky_cb(ev: dict) -> None:
        seen.append(ev)
        if ev.get("type") == "progress":
            raise RuntimeError("simulated WS drop on progress")

    asyncio.run(emit_progress_pair(
        flaky_cb,
        legacy={"type": "dictation_postprocessing"},
        phase=Phase.DICTATION_POST,
        stage=Stage.START,
    ))

    # Both frames were attempted — the helper didn't short-circuit.
    assert len(seen) == 2


def test_emit_pair_passes_payload_through() -> None:
    """Phase-specific payload reaches the progress frame intact."""
    cb, captured = _capturing_on_event()

    asyncio.run(emit_progress_pair(
        cb,
        legacy=None,
        phase=Phase.TOOL,
        stage=Stage.DONE,
        payload={"tool": "datetime", "result": {"now": "2026-04-26"}, "execution_ms": 12},
    ))

    progress = captured[0]
    assert progress["payload"] == {
        "tool": "datetime",
        "result": {"now": "2026-04-26"},
        "execution_ms": 12,
    }


def test_emit_pair_with_error_stage_carries_severity_scope() -> None:
    """Round-trip test: caller passes Severity.FATAL + Scope.LLM
    on an error-stage emit, the resulting frame carries them as
    wire-form strings so Tab5's γ2-H8 router applies."""
    cb, captured = _capturing_on_event()

    asyncio.run(emit_progress_pair(
        cb,
        legacy={"type": "dictation_postprocessing_error", "message": "x"},
        phase=Phase.DICTATION_POST,
        stage=Stage.ERROR,
        code="no_llm_available",
        message="No language model configured.",
        severity=Severity.FATAL,
        scope=Scope.LLM,
    ))

    assert len(captured) == 2
    progress = captured[1]
    assert progress["severity"] == "fatal"
    assert progress["scope"] == "llm"
    assert progress["code"] == "no_llm_available"
    assert progress["stage"] == "error"


def test_emit_pair_orders_match_with_emit_legacy_false_after_legacy_fail() -> None:
    """Edge case: emit_legacy=False AND legacy is provided — the
    legacy must be ignored entirely (not just skipped on send), so
    a flaky legacy can't even be attempted in cleanup mode."""
    legacy_attempts: list[dict] = []

    async def cb(ev: dict) -> None:
        if ev.get("type") == "dictation_postprocessing":
            legacy_attempts.append(ev)
            raise ConnectionError("would have been dropped")

    asyncio.run(emit_progress_pair(
        cb,
        legacy={"type": "dictation_postprocessing"},
        phase=Phase.DICTATION_POST,
        stage=Stage.START,
        emit_legacy=False,
    ))

    # The legacy callback was NEVER invoked — the helper short-
    # circuited the legacy branch entirely, not just on failure.
    assert legacy_attempts == []
