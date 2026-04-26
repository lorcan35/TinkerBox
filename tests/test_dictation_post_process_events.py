"""Unit tests for the dictation post-process event flow.

Phase 2 H4 of the UX-gap remediation (see docs/UX-GAPS.md / issue #94).

Pre-fix: `finish_dictation` emitted `stt` then spawned `_post_process_dictation`
as a background task with no events.  Tab5 sat on a static caption for
10-20 s while the LLM wrote title + summary, then `dictation_summary`
landed without warning.  Failure paths emitted nothing — the UI hung
on "Note saved" forever if the LLM threw or no LLM was available.

This PR adds three events:
  - `dictation_postprocessing` immediately after `stt` so Tab5 can show
    "Generating summary..."
  - `dictation_postprocessing_error` on LLM failure / no-LLM-available
  - `dictation_postprocessing_cancelled` when a new dictation supersedes
    a prior in-flight post-process

These tests exercise each event path with a mock LLM.
"""
from __future__ import annotations

import asyncio

import pytest

from dragon_voice.config import VoiceConfig
from dragon_voice.pipeline import VoicePipeline


# ───────────────────────── helpers


class _StubLLM:
    """Minimal LLM stub — yields a deterministic title/summary response."""

    name = "stub-llm"

    def __init__(self, *, raise_in_stream: type[BaseException] | None = None) -> None:
        self._raise = raise_in_stream

    async def generate_stream(self, prompt: str, system_prompt: str = ""):
        if self._raise:
            raise self._raise("simulated llm failure")
        # Yield a canonical TITLE/SUMMARY response one chunk at a time.
        for chunk in ["TITLE: Test Note\n", "SUMMARY: A test summary."]:
            yield chunk


class _StubConvEngine:
    """Wraps a stub LLM the way ConversationEngine exposes it on .llm"""

    def __init__(self, llm) -> None:
        self.llm = llm


def _make_pipeline(llm=None) -> tuple[VoicePipeline, list[dict]]:
    """Pipeline + an event sink that captures every `_on_event` call."""
    events: list[dict] = []

    async def on_audio(_: bytes) -> None:
        pass

    async def on_event(ev: dict) -> None:
        events.append(ev)

    cfg = VoiceConfig()
    p = VoicePipeline(
        config=cfg,
        on_audio=on_audio,
        on_event=on_event,
        conversation_engine=_StubConvEngine(llm) if llm else None,
        session_id="test-session",
    )
    return p, events


def _types(events: list[dict]) -> list[str]:
    return [e.get("type", "?") for e in events]


def _progress_for_phase(events: list[dict], phase: str) -> list[dict]:
    """β-arch (issue #123): filter the captured events to just the
    ``progress`` frames matching the given phase.  Used by the
    augmented assertions to verify the double-write contract."""
    return [
        e for e in events
        if e.get("type") == "progress" and e.get("phase") == phase
    ]


# ───────────────────────── happy path: postprocessing → summary


def test_finish_dictation_emits_postprocessing_then_summary() -> None:
    p, events = _make_pipeline(llm=_StubLLM())
    # Seed the dictation segments as if process_segment had run.
    p._dictation_segments = ["Hello", "this is a long enough transcript to "
                             "trigger post-processing in the pipeline path."]

    asyncio.run(p.finish_dictation())
    # finish_dictation returns immediately after spawning the post-process
    # task, so we need to wait for it to complete.
    asyncio.run(asyncio.wait_for(p._post_process_task, timeout=2))

    types = _types(events)
    # Order: stt → dictation_postprocessing → dictation_summary
    assert "stt" in types
    assert "dictation_postprocessing" in types
    assert "dictation_summary" in types
    assert types.index("stt") < types.index("dictation_postprocessing")
    assert types.index("dictation_postprocessing") < types.index("dictation_summary")

    # Summary content extracted from the stub response
    summary_ev = next(e for e in events if e["type"] == "dictation_summary")
    assert summary_ev["title"] == "Test Note"
    assert summary_ev["summary"] == "A test summary."

    # β-arch (issue #123): the legacy frames above MUST also be
    # accompanied by progress.dictation_post frames — pin the
    # double-write contract so a future cleanup PR that removes
    # the legacy emit can still rely on the progress channel.
    progress = _progress_for_phase(events, "dictation_post")
    stages = [e["stage"] for e in progress]
    assert "start" in stages, f"missing dictation_post.start; got {stages}"
    assert "done" in stages, f"missing dictation_post.done; got {stages}"
    done_ev = next(e for e in progress if e["stage"] == "done")
    assert done_ev["payload"]["title"] == "Test Note"
    assert done_ev["payload"]["summary"] == "A test summary."


# ───────────────────────── error path: LLM raises


def test_finish_dictation_emits_postprocessing_error_on_llm_failure() -> None:
    p, events = _make_pipeline(llm=_StubLLM(raise_in_stream=RuntimeError))
    p._dictation_segments = ["This dictation is long enough to trigger "
                             "post-processing but the LLM will fail."]

    asyncio.run(p.finish_dictation())
    asyncio.run(asyncio.wait_for(p._post_process_task, timeout=2))

    types = _types(events)
    assert "dictation_postprocessing" in types
    # Error event instead of summary
    assert "dictation_postprocessing_error" in types
    assert "dictation_summary" not in types

    err_ev = next(e for e in events if e["type"] == "dictation_postprocessing_error")
    assert err_ev["error"] == "RuntimeError"
    assert "Note saved" in err_ev["message"]

    # β-arch (issue #123): pair-emit must include a progress.error
    # frame carrying the γ1 taxonomy so γ2-H8 routing applies on
    # an updated Tab5 firmware.
    progress = _progress_for_phase(events, "dictation_post")
    err_progress = [e for e in progress if e["stage"] == "error"]
    assert len(err_progress) == 1
    assert err_progress[0]["code"] == "RuntimeError"
    assert err_progress[0]["severity"] == "transient"
    assert err_progress[0]["scope"] == "llm"


# ───────────────────────── error path: no LLM available


def test_finish_dictation_emits_error_when_no_llm_available() -> None:
    # No conversation_engine + no self._llm → post_process bails early
    p, events = _make_pipeline(llm=None)
    p._dictation_segments = ["This dictation is long enough to trigger "
                             "post-processing but no LLM is configured."]

    asyncio.run(p.finish_dictation())
    asyncio.run(asyncio.wait_for(p._post_process_task, timeout=2))

    types = _types(events)
    assert "dictation_postprocessing" in types
    assert "dictation_postprocessing_error" in types
    err_ev = next(e for e in events if e["type"] == "dictation_postprocessing_error")
    assert err_ev["error"] == "no_llm_available"
    assert "LLM offline" in err_ev["message"]

    # β-arch (issue #123): pair-emit progress.error mirrors the
    # legacy err_ev with code='no_llm_available'.
    progress = _progress_for_phase(events, "dictation_post")
    err_progress = [e for e in progress if e["stage"] == "error"]
    assert len(err_progress) == 1
    assert err_progress[0]["code"] == "no_llm_available"
    assert err_progress[0]["scope"] == "llm"


# ───────────────────────── cancellation path


def test_rapid_finish_dictation_emits_cancelled_for_prior() -> None:
    """Two finish_dictation calls in quick succession should:
      1. Spawn first post-process task
      2. Cancel it on the second call
      3. Emit `dictation_postprocessing_cancelled` so Tab5 knows the
         first summary won't arrive
      4. Spawn the second post-process and emit a fresh
         `dictation_postprocessing` for it
    """
    # Use a slow stub LLM so the first post-process is still running when
    # we kick off the second.
    class _SlowLLM:
        name = "slow"
        async def generate_stream(self, prompt, system_prompt=""):
            await asyncio.sleep(5)  # never completes within the test
            yield ""

    p, events = _make_pipeline(llm=_SlowLLM())

    async def go() -> None:
        # First dictation
        p._dictation_segments = ["First long dictation transcript blah blah."]
        await p.finish_dictation()
        first_task = p._post_process_task
        assert first_task is not None and not first_task.done()
        # Second dictation immediately
        p._dictation_segments = ["Second dictation supersedes the first."]
        await p.finish_dictation()
        second_task = p._post_process_task
        assert second_task is not first_task
        # Wait for first to actually surface its CancelledError
        await asyncio.gather(first_task, second_task, return_exceptions=True)

    asyncio.run(go())

    types = _types(events)
    # Two stt events (one per finish_dictation), one cancellation, two
    # postprocessing starts (one per dictation that was long enough to
    # trigger post-process).
    assert types.count("stt") == 2
    assert types.count("dictation_postprocessing") == 2
    assert types.count("dictation_postprocessing_cancelled") == 1
    # Order: first stt → first postprocessing → second stt → cancelled →
    # second postprocessing
    cancel_idx = types.index("dictation_postprocessing_cancelled")
    second_stt_idx = [i for i, t in enumerate(types) if t == "stt"][1]
    assert cancel_idx > second_stt_idx, (
        "cancelled must be emitted AFTER the new dictation's stt, so Tab5 "
        "can correlate it to the prior turn"
    )

    # β-arch (issue #123): pair-emit must include a single
    # progress.cancelled frame for the prior post-process and TWO
    # progress.start frames (one per dictation that was long enough
    # to trigger post-process).
    progress = _progress_for_phase(events, "dictation_post")
    stages = [e["stage"] for e in progress]
    assert stages.count("start") == 2
    assert stages.count("cancelled") == 1
    cancelled = next(e for e in progress if e["stage"] == "cancelled")
    assert cancelled["code"] == "dictation_post_cancelled"


# ───────────────────────── short-dictation guard (existing behavior)


def test_short_dictation_does_not_post_process() -> None:
    """The existing `len(full_text) > 20` guard means short dictations
    skip post-process entirely — verify we don't emit phantom events."""
    p, events = _make_pipeline(llm=_StubLLM())
    p._dictation_segments = ["short"]  # < 20 chars

    asyncio.run(p.finish_dictation())
    # No post_process_task spawned
    assert p._post_process_task is None

    types = _types(events)
    assert "stt" in types
    # Crucially: no postprocessing event for short dictations
    assert "dictation_postprocessing" not in types
    assert "dictation_postprocessing_error" not in types
    assert "dictation_postprocessing_cancelled" not in types

    # β-arch (issue #123): same regression guard for the new bus —
    # short dictations must NOT emit any progress.dictation_post
    # frames either (otherwise we'd be silently inflating the bus).
    assert _progress_for_phase(events, "dictation_post") == []
