"""Tests for `dragon_voice.start_handler.handle_start_command`.

Wave 6-A of the cross-stack cohesion audit (2026-05-11).  Pin
every behaviour the inline branch had before extraction:

  * Pipeline missing → no-op (no exception, no state mutation)
  * Default mode is "ask" when cmd has no `mode` field
  * `mode="dictate"` resets the segment buffers too
  * `conn_state["mode"]` and `conn_state["turn_id"]` get stashed
  * Missing turn_id → "-" sentinel (W4-B contract)
  * `_audio_buffer.clear()` always called

Run:
    python3 -m pytest tests/test_start_handler.py -v
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

import pytest

from dragon_voice.start_handler import handle_start_command


def _make_pipeline() -> MagicMock:
    pipeline = MagicMock()
    pipeline._audio_buffer = MagicMock()
    pipeline._segment_buffer = MagicMock()
    pipeline._dictation_segments = MagicMock()
    pipeline._dictation_mode = False
    return pipeline


class TestHandleStartCommand:
    @pytest.mark.asyncio
    async def test_no_pipeline_is_noop(self):
        """Pipeline missing (register hasn't completed) → bail
        silently.  Pre-extract this was an implicit `if pipeline:`
        guard."""
        conn_state = {"pipeline": None}
        await handle_start_command("ws0", conn_state, {"mode": "ask"})
        # No exception, no state mutation
        assert "mode" not in conn_state
        assert "turn_id" not in conn_state

    @pytest.mark.asyncio
    async def test_default_mode_is_ask(self):
        pipeline = _make_pipeline()
        conn_state = {"pipeline": pipeline}
        await handle_start_command("ws0", conn_state, {})
        assert conn_state["mode"] == "ask"
        assert pipeline._dictation_mode is False
        pipeline._audio_buffer.clear.assert_called_once()
        # ask mode shouldn't touch segment buffers
        pipeline._segment_buffer.clear.assert_not_called()
        pipeline._dictation_segments.clear.assert_not_called()

    @pytest.mark.asyncio
    async def test_dictate_mode_resets_segment_buffers(self):
        pipeline = _make_pipeline()
        conn_state = {"pipeline": pipeline}
        await handle_start_command("ws0", conn_state, {"mode": "dictate"})
        assert conn_state["mode"] == "dictate"
        assert pipeline._dictation_mode is True
        pipeline._audio_buffer.clear.assert_called_once()
        pipeline._segment_buffer.clear.assert_called_once()
        pipeline._dictation_segments.clear.assert_called_once()

    @pytest.mark.asyncio
    async def test_turn_id_stashed_when_present(self):
        """W4-B contract: explicit turn_id from Tab5 lands on conn_state."""
        pipeline = _make_pipeline()
        conn_state = {"pipeline": pipeline}
        await handle_start_command(
            "ws0",
            conn_state,
            {"mode": "ask", "turn_id": "a1b2c3d4e5f6"},
        )
        assert conn_state["turn_id"] == "a1b2c3d4e5f6"

    @pytest.mark.asyncio
    async def test_turn_id_default_dash_when_missing(self):
        """Pre-W4-A firmwares + warm-boot before first turn → no
        `turn_id` in cmd → falls back to '-' so logs stay greppable."""
        pipeline = _make_pipeline()
        conn_state = {"pipeline": pipeline}
        await handle_start_command("ws0", conn_state, {"mode": "ask"})
        assert conn_state["turn_id"] == "-"

    @pytest.mark.asyncio
    async def test_turn_id_explicit_empty_string_falls_back(self):
        """`turn_id=""` is just as bad as missing — treat as '-'."""
        pipeline = _make_pipeline()
        conn_state = {"pipeline": pipeline}
        await handle_start_command("ws0", conn_state, {"turn_id": ""})
        assert conn_state["turn_id"] == "-"


if __name__ == "__main__":
    unittest.main()
