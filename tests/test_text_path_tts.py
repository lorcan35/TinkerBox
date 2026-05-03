"""Tests for ``dragon_voice.text_path_tts.synthesize_and_stream_text_response``.

Pin every guard branch + happy path + the L3 zombie-kill on
timeout invariant + the F5 TTS receipt + the always-tts_end
contract.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dragon_voice.text_path_tts import (
    _resolve_tts_timeout,
    synthesize_and_stream_text_response,
)


def _make_ws(*, closed: bool = False) -> MagicMock:
    ws = MagicMock()
    ws.closed = closed
    ws.send_json = AsyncMock()
    ws.send_bytes = AsyncMock()
    return ws


def _make_safe_send_json() -> AsyncMock:
    return AsyncMock(return_value=True)


def _make_pipeline_with_tts(
    *,
    sample_rate: int = 22050,
    audio_bytes: bytes = b"\x00\x01" * 1000,
    synthesize_raises: Exception | None = None,
    has_kill_active_procs: bool = True,
) -> MagicMock:
    p = MagicMock()
    tts = MagicMock()
    tts.sample_rate = sample_rate
    if synthesize_raises is not None:
        tts.synthesize = AsyncMock(side_effect=synthesize_raises)
    else:
        tts.synthesize = AsyncMock(return_value=audio_bytes)
    if has_kill_active_procs:
        tts.kill_active_procs = MagicMock()
    p._tts = tts
    return p


def _make_conn_config(
    *,
    tts_backend: str = "piper",
    sample_rate: int = 16000,
) -> MagicMock:
    cfg = MagicMock()
    cfg.tts.backend = tts_backend
    cfg.audio.input_sample_rate = sample_rate
    return cfg


# ─── _resolve_tts_timeout ────────────────────────────────────────


class TestResolveTtsTimeout:
    def test_local_backends_get_90s_budget(self):
        for backend in ("piper", "kokoro", "edge_tts", "(unknown)"):
            assert _resolve_tts_timeout(backend) == 90

    def test_openrouter_gets_30s_budget(self):
        assert _resolve_tts_timeout("openrouter") == 30


# ─── Precondition guards (no-op branches) ────────────────────────


class TestPreconditionGuards:
    @pytest.mark.asyncio
    async def test_no_pipeline_is_noop(self):
        ws = _make_ws()
        send = _make_safe_send_json()
        await synthesize_and_stream_text_response(
            ws,
            pipeline=None,
            response_text="hello",
            response_mode="always_speak",
            conn_config=_make_conn_config(),
            safe_send_json=send,
        )
        ws.send_json.assert_not_awaited()
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_tts_on_pipeline_is_noop(self):
        """Pipeline initialised without TTS (e.g. some test paths)."""
        ws = _make_ws()
        pipeline = MagicMock()
        pipeline._tts = None
        await synthesize_and_stream_text_response(
            ws,
            pipeline=pipeline,
            response_text="hello",
            response_mode="always_speak",
            conn_config=_make_conn_config(),
            safe_send_json=_make_safe_send_json(),
        )
        ws.send_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_whitespace_only_response_is_noop(self):
        ws = _make_ws()
        pipeline = _make_pipeline_with_tts()
        await synthesize_and_stream_text_response(
            ws,
            pipeline=pipeline,
            response_text="   \n   ",
            response_mode="always_speak",
            conn_config=_make_conn_config(),
            safe_send_json=_make_safe_send_json(),
        )
        ws.send_json.assert_not_awaited()
        pipeline._tts.synthesize.assert_not_called()

    @pytest.mark.asyncio
    async def test_ws_closed_is_noop(self):
        ws = _make_ws(closed=True)
        pipeline = _make_pipeline_with_tts()
        await synthesize_and_stream_text_response(
            ws,
            pipeline=pipeline,
            response_text="hello",
            response_mode="always_speak",
            conn_config=_make_conn_config(),
            safe_send_json=_make_safe_send_json(),
        )
        pipeline._tts.synthesize.assert_not_called()

    @pytest.mark.asyncio
    async def test_match_input_response_mode_skips_tts(self):
        """When Tab5 set response_mode='match_input', it told
        Dragon NOT to speak the reply.  Pin the skip."""
        ws = _make_ws()
        pipeline = _make_pipeline_with_tts()
        await synthesize_and_stream_text_response(
            ws,
            pipeline=pipeline,
            response_text="hello",
            response_mode="match_input",
            conn_config=_make_conn_config(),
            safe_send_json=_make_safe_send_json(),
        )
        ws.send_json.assert_not_awaited()
        pipeline._tts.synthesize.assert_not_called()


# ─── Happy path: synth → stream → receipt ────────────────────────


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_emits_tts_start_synth_chunks_tts_end_and_receipt(self):
        ws = _make_ws()
        pipeline = _make_pipeline_with_tts(
            audio_bytes=b"\x00" * (4096 * 5),  # 5 chunks of audio
        )
        send = _make_safe_send_json()

        # Mock resample to return audio unchanged (skip the actual
        # resample work).
        with patch(
            "dragon_voice.text_path_tts.resample_pcm16_async",
            new=AsyncMock(side_effect=lambda b, sr_in, sr_out: b),
        ):
            await synthesize_and_stream_text_response(
                ws,
                pipeline=pipeline,
                response_text="hello",
                response_mode="always_speak",
                conn_config=_make_conn_config(tts_backend="piper"),
                safe_send_json=send,
            )

        # Sequence: tts_start → audio bytes (5 chunks) → tts_end
        json_types = [c.args[0]["type"] for c in ws.send_json.await_args_list]
        assert json_types == ["tts_start", "tts_end"]
        # Audio bytes streamed
        assert ws.send_bytes.await_count == 5
        # F5 receipt fired via safe_send_json
        send.assert_awaited_once()
        receipt = send.await_args.args[1]
        assert receipt["type"] == "receipt"
        assert receipt["stage"] == "tts"
        assert receipt["model"] == "piper"
        assert receipt["cost_mils"] == 0
        assert "tts_ms" in receipt

    @pytest.mark.asyncio
    async def test_empty_audio_skips_byte_stream_but_still_sends_tts_end(self):
        ws = _make_ws()
        pipeline = _make_pipeline_with_tts(audio_bytes=b"")
        send = _make_safe_send_json()

        await synthesize_and_stream_text_response(
            ws,
            pipeline=pipeline,
            response_text="hello",
            response_mode="always_speak",
            conn_config=_make_conn_config(),
            safe_send_json=send,
        )

        ws.send_bytes.assert_not_awaited()
        json_types = [c.args[0]["type"] for c in ws.send_json.await_args_list]
        assert json_types == ["tts_start", "tts_end"]
        # Receipt still emits
        send.assert_awaited_once()


# ─── Timeout / failure with L3 zombie kill ──────────────────────


class TestTimeoutAndFailureBranches:
    @pytest.mark.asyncio
    async def test_timeout_kills_piper_procs_and_sends_tts_end_zero(self):
        """L3 invariant: an asyncio.TimeoutError MUST trigger
        kill_active_procs (no zombie-Piper leak) AND tts_end with
        tts_ms=0 (Tab5 doesn't hang in SPEAKING)."""
        ws = _make_ws()
        pipeline = _make_pipeline_with_tts(
            synthesize_raises=asyncio.TimeoutError(),
        )
        send = _make_safe_send_json()

        await synthesize_and_stream_text_response(
            ws,
            pipeline=pipeline,
            response_text="hello",
            response_mode="always_speak",
            conn_config=_make_conn_config(),
            safe_send_json=send,
        )

        # kill_active_procs called
        pipeline._tts.kill_active_procs.assert_called_once()
        # tts_start sent + tts_end (with 0 ms) sent
        json_types = [c.args[0]["type"] for c in ws.send_json.await_args_list]
        assert "tts_end" in json_types
        last_json = ws.send_json.await_args_list[-1].args[0]
        assert last_json == {"type": "tts_end", "tts_ms": 0}

    @pytest.mark.asyncio
    async def test_synth_exception_also_kills_and_sends_tts_end(self):
        ws = _make_ws()
        pipeline = _make_pipeline_with_tts(
            synthesize_raises=RuntimeError("synth blew up"),
        )

        await synthesize_and_stream_text_response(
            ws,
            pipeline=pipeline,
            response_text="hello",
            response_mode="always_speak",
            conn_config=_make_conn_config(),
            safe_send_json=_make_safe_send_json(),
        )

        pipeline._tts.kill_active_procs.assert_called_once()
        last_json = ws.send_json.await_args_list[-1].args[0]
        assert last_json == {"type": "tts_end", "tts_ms": 0}

    @pytest.mark.asyncio
    async def test_kill_active_procs_failure_does_not_propagate(self):
        """If kill_active_procs itself raises (rare), we still
        send tts_end so Tab5 doesn't hang."""
        ws = _make_ws()
        pipeline = _make_pipeline_with_tts(
            synthesize_raises=asyncio.TimeoutError(),
        )
        pipeline._tts.kill_active_procs = MagicMock(
            side_effect=RuntimeError("kill failed"),
        )

        # Must NOT raise.
        await synthesize_and_stream_text_response(
            ws,
            pipeline=pipeline,
            response_text="hello",
            response_mode="always_speak",
            conn_config=_make_conn_config(),
            safe_send_json=_make_safe_send_json(),
        )

        # tts_end still sent
        last_json = ws.send_json.await_args_list[-1].args[0]
        assert last_json == {"type": "tts_end", "tts_ms": 0}

    @pytest.mark.asyncio
    async def test_tts_without_kill_active_procs_skips_kill(self):
        """If the active TTS backend doesn't expose kill_active_procs
        (e.g. cloud OpenRouter — no subprocess to kill), skip
        cleanly without AttributeError."""
        ws = _make_ws()
        pipeline = _make_pipeline_with_tts(
            synthesize_raises=asyncio.TimeoutError(),
            has_kill_active_procs=False,
        )
        # Remove the attribute via spec
        pipeline._tts = MagicMock(spec=["sample_rate", "synthesize"])
        pipeline._tts.synthesize = AsyncMock(side_effect=asyncio.TimeoutError())
        pipeline._tts.sample_rate = 22050

        await synthesize_and_stream_text_response(
            ws,
            pipeline=pipeline,
            response_text="hello",
            response_mode="always_speak",
            conn_config=_make_conn_config(tts_backend="openrouter"),
            safe_send_json=_make_safe_send_json(),
        )

        last_json = ws.send_json.await_args_list[-1].args[0]
        assert last_json == {"type": "tts_end", "tts_ms": 0}


# ─── Cloud vs local timeout budget ───────────────────────────────


class TestTimeoutBudget:
    @pytest.mark.asyncio
    async def test_openrouter_uses_30s_timeout(self):
        """Pin the per-backend budget by capturing the timeout
        kwarg passed to asyncio.wait_for."""
        ws = _make_ws()
        pipeline = _make_pipeline_with_tts()

        captured = {}
        original_wait_for = asyncio.wait_for

        async def capture_timeout(coro, timeout):
            captured["timeout"] = timeout
            return await original_wait_for(coro, timeout)

        with patch(
            "dragon_voice.text_path_tts.resample_pcm16_async",
            new=AsyncMock(side_effect=lambda b, sr_in, sr_out: b),
        ), patch(
            "dragon_voice.text_path_tts.asyncio.wait_for",
            new=capture_timeout,
        ):
            await synthesize_and_stream_text_response(
                ws,
                pipeline=pipeline,
                response_text="hi",
                response_mode="always_speak",
                conn_config=_make_conn_config(tts_backend="openrouter"),
                safe_send_json=_make_safe_send_json(),
            )

        assert captured["timeout"] == 30

    @pytest.mark.asyncio
    async def test_piper_uses_90s_timeout(self):
        ws = _make_ws()
        pipeline = _make_pipeline_with_tts()
        captured = {}
        original_wait_for = asyncio.wait_for

        async def capture_timeout(coro, timeout):
            captured["timeout"] = timeout
            return await original_wait_for(coro, timeout)

        with patch(
            "dragon_voice.text_path_tts.resample_pcm16_async",
            new=AsyncMock(side_effect=lambda b, sr_in, sr_out: b),
        ), patch(
            "dragon_voice.text_path_tts.asyncio.wait_for",
            new=capture_timeout,
        ):
            await synthesize_and_stream_text_response(
                ws,
                pipeline=pipeline,
                response_text="hi",
                response_mode="always_speak",
                conn_config=_make_conn_config(tts_backend="piper"),
                safe_send_json=_make_safe_send_json(),
            )

        assert captured["timeout"] == 90
