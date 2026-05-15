"""Tests for ``dragon_voice.dictation_post``.

Pin every event-emit branch + the LLM-resolution contract +
the title/summary parser defaults.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.dictation_post import (
    _parse_title_summary,
    run_dictation_post_process,
)


def _make_llm(*, response_tokens: list[str] | None = None) -> MagicMock:
    llm = MagicMock()

    async def _stream(prompt, system_prompt):
        for tok in (response_tokens or []):
            yield tok

    llm.generate_stream = _stream
    return llm


def _make_on_event() -> AsyncMock:
    return AsyncMock()


def _emitted_types(on_event: AsyncMock) -> list[str]:
    """Extract `type` from each event sent (legacy frames)."""
    out = []
    for c in on_event.await_args_list:
        payload = c.args[0]
        if "type" in payload:
            out.append(payload["type"])
    return out


# ─── No-LLM branch ──────────────────────────────────────────


class TestNoLLM:
    @pytest.mark.asyncio
    async def test_no_llm_emits_error_event_and_returns(self):
        """Phase 2 H4 (#94) closure pin: if no LLM is available,
        we MUST emit `dictation_postprocessing_error` so Tab5
        clears the 'Generating summary...' caption."""
        on_event = _make_on_event()

        await run_dictation_post_process(
            "some transcript that's long enough to matter",
            llm=None,
            on_event=on_event,
            emit_legacy=True,
        )

        types = _emitted_types(on_event)
        assert "dictation_postprocessing_error" in types
        # The legacy frame's `error` field carries the code
        for c in on_event.await_args_list:
            payload = c.args[0]
            if payload.get("type") == "dictation_postprocessing_error":
                assert payload["error"] == "no_llm_available"
                assert "LLM offline" in payload["message"]


# ─── Happy path ─────────────────────────────────────────────


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_emits_dictation_summary_with_parsed_title(self):
        on_event = _make_on_event()
        llm = _make_llm(response_tokens=[
            "TITLE: ", "Quick ", "Note\n",
            "SUMMARY: ", "User said hello.",
        ])

        await run_dictation_post_process(
            "User said hello to the assistant.",
            llm=llm,
            on_event=on_event,
            emit_legacy=True,
        )

        # Find the dictation_summary frame
        summary_frame = next(
            c.args[0] for c in on_event.await_args_list
            if c.args[0].get("type") == "dictation_summary"
        )
        assert summary_frame["title"] == "Quick Note"
        assert summary_frame["summary"] == "User said hello."

    @pytest.mark.asyncio
    async def test_malformed_response_uses_defaults(self):
        """Pin: when the LLM goes off-format (no TITLE/SUMMARY
        markers), defaults to 'Untitled Note' + first 200 chars
        of transcript."""
        on_event = _make_on_event()
        # LLM responds with prose instead of TITLE:/SUMMARY:
        llm = _make_llm(response_tokens=[
            "Sure, here's a summary: the user spoke.",
        ])

        transcript = "User said hello to the assistant in a clear voice."
        await run_dictation_post_process(
            transcript,
            llm=llm,
            on_event=on_event,
            emit_legacy=True,
        )

        summary_frame = next(
            c.args[0] for c in on_event.await_args_list
            if c.args[0].get("type") == "dictation_summary"
        )
        assert summary_frame["title"] == "Untitled Note"
        # Summary defaults to first 200 chars of transcript
        assert summary_frame["summary"] == transcript[:200]


# ─── Failure branch ─────────────────────────────────────────


class TestFailure:
    @pytest.mark.asyncio
    async def test_llm_exception_emits_error_event(self):
        on_event = _make_on_event()
        llm = MagicMock()

        async def _broken(prompt, system_prompt):
            raise RuntimeError("LLM crashed mid-summary")
            yield  # pragma: no cover

        llm.generate_stream = _broken

        await run_dictation_post_process(
            "transcript",
            llm=llm,
            on_event=on_event,
            emit_legacy=True,
        )

        # Error event emitted with exception class name as code
        err_frame = next(
            c.args[0] for c in on_event.await_args_list
            if c.args[0].get("type") == "dictation_postprocessing_error"
        )
        assert err_frame["error"] == "RuntimeError"
        assert "summary generation failed" in err_frame["message"]

    @pytest.mark.asyncio
    async def test_cancellederror_propagates(self):
        """asyncio.CancelledError MUST re-propagate so the task
        transitions to CANCELLED state (the cancelled-side
        event was already emitted by `finish_dictation` before
        spawning this task — no double-emit)."""
        on_event = _make_on_event()
        llm = MagicMock()

        async def _cancel(prompt, system_prompt):
            raise asyncio.CancelledError()
            yield  # pragma: no cover

        llm.generate_stream = _cancel

        with pytest.raises(asyncio.CancelledError):
            await run_dictation_post_process(
                "x",
                llm=llm,
                on_event=on_event,
                emit_legacy=True,
            )

        # No error event emitted — caller already handled the
        # cancellation event before spawning this task.
        types = _emitted_types(on_event)
        assert "dictation_postprocessing_error" not in types
        assert "dictation_summary" not in types


# ─── Title/Summary parser ──────────────────────────────────


class TestParser:
    def test_canonical_format_parses_cleanly(self):
        title, summary = _parse_title_summary(
            "TITLE: My Note\nSUMMARY: A short summary.",
            "fallback",
        )
        assert title == "My Note"
        assert summary == "A short summary."

    def test_quoted_values_have_quotes_stripped(self):
        """Some LLMs wrap the values in quotes — strip 'em."""
        title, summary = _parse_title_summary(
            'TITLE: "Quoted Title"\nSUMMARY: "Quoted summary."',
            "fallback",
        )
        assert title == "Quoted Title"
        assert summary == "Quoted summary."

    def test_case_insensitive_marker_match(self):
        title, summary = _parse_title_summary(
            "title: lowercase\nsummary: works",
            "fallback",
        )
        assert title == "lowercase"
        assert summary == "works"

    def test_only_title_present_summary_defaults(self):
        title, summary = _parse_title_summary(
            "TITLE: Just a title",
            "fallback transcript content",
        )
        assert title == "Just a title"
        # Summary defaults to first 200 chars of transcript
        assert summary == "fallback transcript content"

    def test_only_summary_present_title_defaults(self):
        title, summary = _parse_title_summary(
            "SUMMARY: Just a summary",
            "fallback",
        )
        assert title == "Untitled Note"
        assert summary == "Just a summary"

    def test_empty_response_uses_all_defaults(self):
        title, summary = _parse_title_summary(
            "",
            "fallback transcript",
        )
        assert title == "Untitled Note"
        assert summary == "fallback transcript"

    def test_summary_truncated_to_200_when_using_default(self):
        long_transcript = "x" * 500
        title, summary = _parse_title_summary("", long_transcript)
        assert len(summary) == 200


# ─── emit_legacy flag ──────────────────────────────────────


class TestEmitLegacyFlag:
    @pytest.mark.asyncio
    async def test_emit_legacy_false_skips_legacy_frame(self):
        """When emit_legacy=False (β-arch β2 cutover complete),
        only the new progress.* frames fire — NOT the legacy
        `dictation_summary` frame."""
        on_event = _make_on_event()
        llm = _make_llm(response_tokens=[
            "TITLE: x\nSUMMARY: y",
        ])

        await run_dictation_post_process(
            "transcript",
            llm=llm,
            on_event=on_event,
            emit_legacy=False,
        )

        types = _emitted_types(on_event)
        # Legacy frame should NOT be present; progress.* frame should
        assert "dictation_summary" not in types


# ── PR 2 polish: synthesized Local-mode title/summary ────────────────────

from dragon_voice.dictation_post import _synthesize_local_title_summary


def test_synthesize_local_long_transcript():
    """Title is first ~50 chars at word boundary, capitalized; summary
    is full transcript up to 200 chars."""
    t = "hello world this is a test of long-form dictation across the network"
    title, summary = _synthesize_local_title_summary(t)
    assert title.startswith("Hello world")
    assert len(title) <= 50
    assert " " in title  # word boundary, not mid-word
    assert summary == t  # under 200 chars, returned as-is


def test_synthesize_local_short_transcript():
    """Short transcripts: title is the whole transcript (capitalized)."""
    title, summary = _synthesize_local_title_summary("short note")
    assert title == "Short note"
    assert summary == "short note"


def test_synthesize_local_empty_transcript():
    """Empty or whitespace transcript falls back to 'Untitled'."""
    for empty in ("", "   ", "\n\t"):
        title, summary = _synthesize_local_title_summary(empty)
        assert title == "Untitled"
        assert summary == "Untitled"


def test_synthesize_local_long_summary_truncated_with_ellipsis():
    """Summaries beyond 200 chars are truncated with an ellipsis."""
    t = "word " * 100  # 500 chars
    _title, summary = _synthesize_local_title_summary(t)
    assert len(summary) <= 201  # 200 chars + 1-char ellipsis
    assert summary.endswith("…")


def test_synthesize_local_word_boundary_in_title():
    """Title truncation respects word boundaries (no mid-word cuts)."""
    t = "supercalifragilisticexpialidocious is a long word that should be word-broken"
    title, _summary = _synthesize_local_title_summary(t)
    # Either the whole long word fits, or we stop before it.
    # Either way, no mid-word truncation.
    assert title == "Supercalifragilisticexpialidocious" or \
           not title.endswith(("supercalif", "supercalifragili"))


@pytest.mark.asyncio
async def test_ollama_backend_bypasses_llm_for_synthesis():
    """When the LLM backend is OllamaBackend, dictation_post should
    skip the LLM call entirely and emit a dictation_summary built from
    the transcript directly (no slow CPU LLM round-trip)."""
    events: list[dict] = []

    async def on_event(e: dict) -> None:
        events.append(e)

    # Mock that *looks* like OllamaBackend by name — that's the gate
    # we use to distinguish Local mode from Solo/Cloud (which use
    # OpenRouter and are fast enough to call directly).
    llm = MagicMock()
    llm.__class__.__name__ = "OllamaBackend"

    await run_dictation_post_process(
        "hello world testing cross-network dictation",
        llm=llm,
        on_event=on_event,
        emit_legacy=True,
    )

    # The LLM should NOT have been called.
    llm.generate_stream.assert_not_called()

    # dictation_summary event must have been emitted with the
    # synthesized title + non-empty summary.
    summaries = [e for e in events if e.get("type") == "dictation_summary"]
    assert len(summaries) >= 1
    final = summaries[-1]
    assert final["title"].startswith("Hello world")
    assert "cross-network dictation" in final["summary"]
