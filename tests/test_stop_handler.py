"""Tests for ``dragon_voice.stop_handler``.

Pin every branch + the >10-char dictation-note gate.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.stop_handler import handle_stop_command


def _make_ws(*, closed: bool = False) -> MagicMock:
    ws = MagicMock()
    ws.closed = closed
    ws.send_json = AsyncMock()
    return ws


def _make_pipeline(
    *,
    transcript: str = "",
    audio_buffer_len: int = 0,
    segment_buffer_len: int = 0,
) -> MagicMock:
    p = MagicMock()
    p._audio_buffer = bytearray(audio_buffer_len)
    p._segment_buffer = bytearray(segment_buffer_len)
    p.finish_dictation = AsyncMock(return_value=transcript)
    p.start_processing = AsyncMock()
    return p


def _make_notes_svc(*, note_id: str = "note-1", title: str = "T") -> MagicMock:
    svc = MagicMock()
    note = MagicMock()
    note.id = note_id
    note.title = title
    svc.create_from_text = AsyncMock(return_value=note)
    return svc


# ─── No-pipeline boot race ────────────────────────────────────


class TestNoPipeline:
    @pytest.mark.asyncio
    async def test_no_pipeline_is_silent_noop(self):
        ws = _make_ws()
        await handle_stop_command(
            ws,
            ws_id="ws1",
            conn_state={},  # no pipeline
            conn_lock=asyncio.Lock(),
            notes_svc=_make_notes_svc(),
        )
        # Nothing happened; no frame.
        ws.send_json.assert_not_awaited()


# ─── Ask mode ─────────────────────────────────────────────────


class TestAskMode:
    @pytest.mark.asyncio
    async def test_ask_mode_calls_start_processing(self):
        ws = _make_ws()
        pipeline = _make_pipeline()

        await handle_stop_command(
            ws,
            ws_id="ws2",
            conn_state={"pipeline": pipeline, "mode": "ask"},
            conn_lock=asyncio.Lock(),
            notes_svc=_make_notes_svc(),
        )

        pipeline.start_processing.assert_awaited_once()
        pipeline.finish_dictation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_mode_falls_through_to_start_processing(self):
        """Modes other than 'dictate' default to ask-mode behaviour
        (including missing or unrecognised values)."""
        ws = _make_ws()
        pipeline = _make_pipeline()

        await handle_stop_command(
            ws,
            ws_id="ws3",
            conn_state={"pipeline": pipeline},  # no mode key
            conn_lock=asyncio.Lock(),
            notes_svc=_make_notes_svc(),
        )

        pipeline.start_processing.assert_awaited_once()


# ─── Dictate mode ─────────────────────────────────────────────


class TestDictateMode:
    @pytest.mark.asyncio
    async def test_dictate_mode_calls_finish_dictation(self):
        ws = _make_ws()
        pipeline = _make_pipeline(transcript="A long enough transcript")

        await handle_stop_command(
            ws,
            ws_id="ws4",
            conn_state={"pipeline": pipeline, "mode": "dictate"},
            conn_lock=asyncio.Lock(),
            notes_svc=_make_notes_svc(),
        )

        pipeline.finish_dictation.assert_awaited_once()
        pipeline.start_processing.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dictate_long_transcript_creates_note_and_emits_frame(self):
        ws = _make_ws()
        pipeline = _make_pipeline(transcript="This is a real dictation.")
        notes_svc = _make_notes_svc(note_id="note-42", title="Auto Title")

        await handle_stop_command(
            ws,
            ws_id="ws5",
            conn_state={"pipeline": pipeline, "mode": "dictate"},
            conn_lock=asyncio.Lock(),
            notes_svc=notes_svc,
        )

        notes_svc.create_from_text.assert_awaited_once()
        ws.send_json.assert_awaited_once()
        frame = ws.send_json.await_args.args[0]
        assert frame["type"] == "note_created"
        assert frame["note_id"] == "note-42"
        assert frame["title"] == "Auto Title"
        # Transcript truncated to 200 chars
        assert frame["transcript"] == "This is a real dictation."

    @pytest.mark.asyncio
    async def test_dictate_short_transcript_skips_note(self):
        """≤10 chars → silence-trim residue / filler.  Skip the
        note auto-save so the notes view doesn't fill with junk."""
        ws = _make_ws()
        pipeline = _make_pipeline(transcript="uhh")  # 3 chars
        notes_svc = _make_notes_svc()

        await handle_stop_command(
            ws,
            ws_id="ws6",
            conn_state={"pipeline": pipeline, "mode": "dictate"},
            conn_lock=asyncio.Lock(),
            notes_svc=notes_svc,
        )

        notes_svc.create_from_text.assert_not_awaited()
        # No note_created frame
        ws.send_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dictate_whitespace_only_transcript_skips_note(self):
        ws = _make_ws()
        pipeline = _make_pipeline(transcript="   \n\t   ")
        notes_svc = _make_notes_svc()

        await handle_stop_command(
            ws,
            ws_id="ws7",
            conn_state={"pipeline": pipeline, "mode": "dictate"},
            conn_lock=asyncio.Lock(),
            notes_svc=notes_svc,
        )

        notes_svc.create_from_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dictate_empty_transcript_skips_note(self):
        ws = _make_ws()
        pipeline = _make_pipeline(transcript="")
        notes_svc = _make_notes_svc()

        await handle_stop_command(
            ws,
            ws_id="ws8",
            conn_state={"pipeline": pipeline, "mode": "dictate"},
            conn_lock=asyncio.Lock(),
            notes_svc=notes_svc,
        )

        notes_svc.create_from_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dictate_no_notes_svc_skips_note(self):
        """No notes service (test path / boot race) → finish_dictation
        still runs but auto-note silently skips."""
        ws = _make_ws()
        pipeline = _make_pipeline(transcript="A long enough transcript")

        await handle_stop_command(
            ws,
            ws_id="ws9",
            conn_state={"pipeline": pipeline, "mode": "dictate"},
            conn_lock=asyncio.Lock(),
            notes_svc=None,
        )

        pipeline.finish_dictation.assert_awaited_once()
        # No note_created frame
        ws.send_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dictate_create_from_text_failure_is_swallowed(self):
        """Note auto-create failure (DB hiccup, embedder timeout)
        MUST NOT fail the stop command — the transcript already
        landed via the pipeline's dictation_summary frame."""
        ws = _make_ws()
        pipeline = _make_pipeline(transcript="This is a real dictation.")
        notes_svc = MagicMock()
        notes_svc.create_from_text = AsyncMock(
            side_effect=RuntimeError("DB down"),
        )

        # Must NOT raise.
        await handle_stop_command(
            ws,
            ws_id="ws10",
            conn_state={"pipeline": pipeline, "mode": "dictate"},
            conn_lock=asyncio.Lock(),
            notes_svc=notes_svc,
        )

        # No note_created emitted on the failure path
        ws.send_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dictate_ws_closed_skips_frame_emit_but_creates_note(self):
        """When ws closes between create_from_text and frame emit,
        the note is still saved (it's persisted DB state) but the
        frame isn't sent."""
        ws = _make_ws(closed=True)
        pipeline = _make_pipeline(transcript="A long enough transcript")
        notes_svc = _make_notes_svc()

        await handle_stop_command(
            ws,
            ws_id="ws11",
            conn_state={"pipeline": pipeline, "mode": "dictate"},
            conn_lock=asyncio.Lock(),
            notes_svc=notes_svc,
        )

        # Note still created (DB persistence)
        notes_svc.create_from_text.assert_awaited_once()
        # But no frame emitted (ws closed)
        ws.send_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dictate_transcript_truncated_to_200_chars(self):
        """The note_created frame's transcript is capped at 200
        chars to keep the WS payload small.  Pin the cap so a
        future refactor can't accidentally send the full text."""
        long_transcript = "x" * 500
        ws = _make_ws()
        pipeline = _make_pipeline(transcript=long_transcript)
        notes_svc = _make_notes_svc()

        await handle_stop_command(
            ws,
            ws_id="ws12",
            conn_state={"pipeline": pipeline, "mode": "dictate"},
            conn_lock=asyncio.Lock(),
            notes_svc=notes_svc,
        )

        frame = ws.send_json.await_args.args[0]
        assert len(frame["transcript"]) == 200

    @pytest.mark.asyncio
    async def test_dictate_note_created_carries_turn_id(self):
        """W4: Tab5 reconciles the optimistic note row by turn_id, so the
        note_created frame MUST echo the turn's id (from conn_state, stashed
        there by start_handler).  Pin it so a refactor can't drop it."""
        ws = _make_ws()
        pipeline = _make_pipeline(transcript="A long enough transcript")
        notes_svc = _make_notes_svc(note_id="note-77")

        await handle_stop_command(
            ws,
            ws_id="ws-tid",
            conn_state={"pipeline": pipeline, "mode": "dictate", "turn_id": "abc123def456"},
            conn_lock=asyncio.Lock(),
            notes_svc=notes_svc,
        )

        frame = ws.send_json.await_args.args[0]
        assert frame["type"] == "note_created"
        assert frame["turn_id"] == "abc123def456"

    @pytest.mark.asyncio
    async def test_dictate_note_created_turn_id_defaults_to_dash(self):
        """When conn_state has no turn_id (legacy / boot race), the frame still
        carries a turn_id field ('-') so the Tab5 lookup is well-defined
        (treated as 'no match' → fresh note)."""
        ws = _make_ws()
        pipeline = _make_pipeline(transcript="A long enough transcript")
        notes_svc = _make_notes_svc()

        await handle_stop_command(
            ws,
            ws_id="ws-tid2",
            conn_state={"pipeline": pipeline, "mode": "dictate"},  # no turn_id
            conn_lock=asyncio.Lock(),
            notes_svc=notes_svc,
        )

        frame = ws.send_json.await_args.args[0]
        assert frame["turn_id"] == "-"


# ─── conn_lock serialisation ──────────────────────────────────


class TestConnLockHeld:
    @pytest.mark.asyncio
    async def test_conn_lock_acquired_during_processing(self):
        """The lock MUST be held across the start_processing /
        finish_dictation call so a concurrent text cmd handler
        can't interleave (US-P10)."""
        ws = _make_ws()
        pipeline = _make_pipeline()
        lock = asyncio.Lock()

        # Hold the lock externally; the handler should wait
        # for it before calling start_processing.
        await lock.acquire()
        call_count = {"n": 0}

        async def _record(*args, **kwargs):
            call_count["n"] += 1

        pipeline.start_processing.side_effect = _record

        # Schedule the handler — it should block on the lock.
        task = asyncio.create_task(
            handle_stop_command(
                ws,
                ws_id="ws13",
                conn_state={"pipeline": pipeline, "mode": "ask"},
                conn_lock=lock,
                notes_svc=None,
            )
        )
        await asyncio.sleep(0.01)
        assert call_count["n"] == 0  # blocked on lock

        lock.release()
        await task
        assert call_count["n"] == 1
