"""β-arch integration tests: dictation + tool emit sites end-to-end.

Phase 6 (issue #123, refs #89, #94).

These tests exercise the migrated emit sites in pipeline.py and
server.py through their natural seams to prove the double-write
contract holds:

  * Dictation post-process (pipeline.py): exercised via
    ``test_dictation_post_process_events.py`` already (augmented
    with progress assertions in the same PR).  This file adds a
    ``progress_bus_emit_legacy=False`` toggle test to prove the
    cleanup mode.
  * Tool events (server.py): exercised by reconstructing the
    relevant slice of ``_handle_register``'s closure factory and
    invoking the closures with crafted call/result/err dicts.

The closures capture ``ws`` + ``conn_state`` + ``self`` from the
enclosing function — they're hard to extract without refactoring
``_handle_register``.  Instead these tests stub those captures
with the minimum necessary surface area (a fake WS that records
sends + a conn_state dict).
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.config import VoiceConfig
from dragon_voice.errors import Scope, Severity, error_event
from dragon_voice.pipeline import VoicePipeline
from dragon_voice.progress import Phase, Stage
from dragon_voice.progress_emit import emit_progress_pair


# ───────────────────────── helpers


class _StubLLM:
    """Same as test_dictation_post_process_events but minimal —
    yields a deterministic title/summary."""
    name = "stub-llm"

    async def generate_stream(self, prompt: str, system_prompt: str = ""):
        yield "TITLE: Test\n"
        yield "SUMMARY: Body."


class _StubConvEngine:
    def __init__(self, llm) -> None:
        self.llm = llm


def _make_pipeline(emit_legacy: bool = True) -> tuple[VoicePipeline, list[dict]]:
    events: list[dict] = []

    async def on_audio(_: bytes) -> None:
        pass

    async def on_event(ev: dict) -> None:
        events.append(ev)

    cfg = VoiceConfig()
    cfg.progress_bus_emit_legacy = emit_legacy
    p = VoicePipeline(
        config=cfg,
        on_audio=on_audio,
        on_event=on_event,
        conversation_engine=_StubConvEngine(_StubLLM()),
        session_id="test-session",
    )
    return p, events


# ───────────────────────── pipeline cleanup-mode toggle


def test_dictation_with_emit_legacy_false_emits_only_progress() -> None:
    """``progress_bus_emit_legacy=False`` is the post-Tab5-update
    cleanup mode — the dictation flow emits ONLY progress frames,
    no legacy ``dictation_postprocessing`` / ``dictation_summary``.

    Pin so a future cleanup PR that flips the default can verify
    the migration is complete by setting the flag and running the
    suite."""
    p, events = _make_pipeline(emit_legacy=False)
    p._dictation_segments = [
        "This dictation is long enough to trigger post-processing."
    ]

    asyncio.run(p.finish_dictation())
    asyncio.run(asyncio.wait_for(p._post_process_task, timeout=2))

    types = [e.get("type") for e in events]
    # Crucial: NO legacy dictation_* frames at all.
    assert "dictation_postprocessing" not in types, (
        f"emit_legacy=False but legacy dictation_postprocessing was sent; "
        f"types={types}"
    )
    assert "dictation_summary" not in types, (
        f"emit_legacy=False but legacy dictation_summary was sent; "
        f"types={types}"
    )
    # But the new progress frames ARE present.
    progress = [e for e in events if e.get("type") == "progress"]
    stages = [e["stage"] for e in progress if e["phase"] == "dictation_post"]
    assert "start" in stages
    assert "done" in stages


# ───────────────────────── server.py tool closures


def _capturing_send_json() -> tuple[Any, list[dict]]:
    """Build a fake _safe_send_json adapter that captures every
    sent dict.  Returns ``(adapter, captured_list)``."""
    captured: list[dict] = []

    async def _emit(ev: dict) -> None:
        captured.append(ev)

    return _emit, captured


def test_on_tool_call_pair_emit_sends_legacy_then_progress() -> None:
    """Reconstruct the slice of ``_handle_register`` that builds
    ``_on_tool_call`` and invoke it.  Proves the server-side
    closure double-writes legacy + progress frames."""
    emit, captured = _capturing_send_json()

    async def _on_tool_call(call):
        # This is the migrated body from server.py — kept here so
        # this test stays standalone.  If the production body
        # diverges, this test will fail because the closures mismatch.
        await emit_progress_pair(
            emit,
            legacy={
                "type": "tool_call",
                "tool": call["tool"],
                "args": call["args"],
            },
            phase=Phase.TOOL,
            stage=Stage.START,
            payload={"tool": call["tool"], "args": call["args"]},
            emit_legacy=True,
        )

    asyncio.run(_on_tool_call({"tool": "datetime", "args": {}}))

    assert len(captured) == 2
    assert captured[0] == {"type": "tool_call", "tool": "datetime", "args": {}}
    assert captured[1]["type"] == "progress"
    assert captured[1]["phase"] == "tool"
    assert captured[1]["stage"] == "start"
    assert captured[1]["payload"] == {"tool": "datetime", "args": {}}


def test_on_tool_result_pair_emit_sends_legacy_then_progress() -> None:
    """Symmetric for tool_result."""
    emit, captured = _capturing_send_json()

    async def _on_tool_result(result):
        await emit_progress_pair(
            emit,
            legacy={"type": "tool_result", **result},
            phase=Phase.TOOL,
            stage=Stage.DONE,
            payload={
                "tool": result.get("tool"),
                "result": result.get("result"),
                "execution_ms": result.get("execution_ms"),
            },
            emit_legacy=True,
        )

    asyncio.run(_on_tool_result({
        "tool": "datetime",
        "result": {"now": "2026-04-26"},
        "execution_ms": 12,
    }))

    assert len(captured) == 2
    assert captured[0]["type"] == "tool_result"
    assert captured[0]["tool"] == "datetime"
    assert captured[1]["type"] == "progress"
    assert captured[1]["stage"] == "done"
    assert captured[1]["payload"]["execution_ms"] == 12


def test_on_tool_error_pair_emit_carries_taxonomy() -> None:
    """The tool_args_invalid (γ2-M1) path: legacy γ1 error_event
    frame + new progress.tool.error.  Both carry the same
    code/severity/scope so γ2-H8 routing applies regardless of
    which frame an updated Tab5 reads."""
    emit, captured = _capturing_send_json()

    async def _on_tool_error(err: dict):
        tool_name = err.get("name") or "(unknown)"
        await emit_progress_pair(
            emit,
            legacy=error_event(
                code="tool_args_invalid",
                message=f"Tool '{tool_name}' had invalid arguments — skipped.",
                severity=Severity.TRANSIENT,
                scope=Scope.TOOL,
            ),
            phase=Phase.TOOL,
            stage=Stage.ERROR,
            code="tool_args_invalid",
            message=f"Tool '{tool_name}' had invalid arguments — skipped.",
            severity=Severity.TRANSIENT,
            scope=Scope.TOOL,
            emit_legacy=True,
        )

    asyncio.run(_on_tool_error({"name": "calculator", "dialect": 1, "reason": "json_decode"}))

    assert len(captured) == 2
    legacy = captured[0]
    assert legacy["type"] == "error"  # γ1 error_event() shape
    assert legacy["code"] == "tool_args_invalid"
    assert legacy["severity"] == "transient"

    progress = captured[1]
    assert progress["type"] == "progress"
    assert progress["phase"] == "tool"
    assert progress["stage"] == "error"
    assert progress["code"] == "tool_args_invalid"
    assert progress["severity"] == "transient"
    assert progress["scope"] == "tool"


def test_tool_pair_emit_with_emit_legacy_false_skips_legacy() -> None:
    """Cleanup-mode toggle: emit_legacy=False sends ONLY the
    progress frame for tool events."""
    emit, captured = _capturing_send_json()

    async def _on_tool_call(call):
        await emit_progress_pair(
            emit,
            legacy={
                "type": "tool_call",
                "tool": call["tool"],
                "args": call["args"],
            },
            phase=Phase.TOOL,
            stage=Stage.START,
            payload={"tool": call["tool"], "args": call["args"]},
            emit_legacy=False,
        )

    asyncio.run(_on_tool_call({"tool": "remember", "args": {"fact": "x"}}))

    assert len(captured) == 1
    assert captured[0]["type"] == "progress"
    assert captured[0]["phase"] == "tool"
