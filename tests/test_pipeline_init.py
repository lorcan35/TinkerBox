"""Tests for ``dragon_voice.pipeline_init``.

Pin every branch of the pipeline-build chain so a future
refactor can't accidentally:
  * Drop the local-defaults reset (would leave cloud state
    leaking into the pipeline init)
  * Leak Python exception text into Tab5's voice caption
  * Rebuild VoicePipeline with the wrong tool callbacks
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dragon_voice.errors import Scope, Severity


def _make_ws(*, closed: bool = False) -> MagicMock:
    ws = MagicMock()
    ws.closed = closed
    ws.send_json = AsyncMock()
    return ws


def _make_safe_send_json() -> AsyncMock:
    return AsyncMock(return_value=True)


def _make_conn_config(
    *,
    stt: str = "openrouter",     # cloud — to be reset
    tts: str = "openrouter",     # cloud — to be reset
    llm: str = "openrouter",     # cloud — to be reset
    local_backend: str = "ollama",
) -> MagicMock:
    cfg = MagicMock()
    cfg.stt = MagicMock()
    cfg.stt.backend = stt
    cfg.tts = MagicMock()
    cfg.tts.backend = tts
    cfg.llm = MagicMock()
    cfg.llm.backend = llm
    cfg.llm.local_backend = local_backend
    cfg.llm.system_prompt = ""
    cfg.llm.max_tokens = 0
    return cfg


# ─── Local-defaults reset ────────────────────────────────────


class TestLocalDefaultsReset:
    """The cloud-config-leakage protection."""

    @pytest.mark.asyncio
    async def test_cloud_stt_reset_to_moonshine(self):
        from dragon_voice.pipeline_init import build_and_initialize_pipeline
        cfg = _make_conn_config(stt="openrouter")
        with patch(
            "dragon_voice.pipeline_init.VoicePipeline"
        ) as VP:
            VP.return_value.initialize = AsyncMock()
            await build_and_initialize_pipeline(
                _make_ws(),
                ws_id="ws1",
                conn_config=cfg,
                on_audio=AsyncMock(),
                on_event=AsyncMock(),
                conversation=MagicMock(),
                session_id="s",
                media_pipeline=None,
                backend_pool=None,
            )
        assert cfg.stt.backend == "moonshine"

    @pytest.mark.asyncio
    async def test_cloud_tts_reset_to_piper(self):
        from dragon_voice.pipeline_init import build_and_initialize_pipeline
        cfg = _make_conn_config(tts="openrouter")
        with patch("dragon_voice.pipeline_init.VoicePipeline") as VP:
            VP.return_value.initialize = AsyncMock()
            await build_and_initialize_pipeline(
                _make_ws(),
                ws_id="ws2",
                conn_config=cfg,
                on_audio=AsyncMock(),
                on_event=AsyncMock(),
                conversation=MagicMock(),
                session_id="s",
                media_pipeline=None,
                backend_pool=None,
            )
        assert cfg.tts.backend == "piper"

    @pytest.mark.asyncio
    async def test_cloud_llm_reset_to_local_backend(self):
        from dragon_voice.pipeline_init import build_and_initialize_pipeline
        cfg = _make_conn_config(llm="openrouter", local_backend="lmstudio")
        with patch("dragon_voice.pipeline_init.VoicePipeline") as VP:
            VP.return_value.initialize = AsyncMock()
            await build_and_initialize_pipeline(
                _make_ws(),
                ws_id="ws3",
                conn_config=cfg,
                on_audio=AsyncMock(),
                on_event=AsyncMock(),
                conversation=MagicMock(),
                session_id="s",
                media_pipeline=None,
                backend_pool=None,
            )
        assert cfg.llm.backend == "lmstudio"

    @pytest.mark.asyncio
    async def test_cloud_llm_with_no_local_backend_falls_back_to_ollama(self):
        from dragon_voice.pipeline_init import build_and_initialize_pipeline
        cfg = _make_conn_config(llm="tinkerclaw", local_backend="")
        with patch("dragon_voice.pipeline_init.VoicePipeline") as VP:
            VP.return_value.initialize = AsyncMock()
            await build_and_initialize_pipeline(
                _make_ws(),
                ws_id="ws4",
                conn_config=cfg,
                on_audio=AsyncMock(),
                on_event=AsyncMock(),
                conversation=MagicMock(),
                session_id="s",
                media_pipeline=None,
                backend_pool=None,
            )
        assert cfg.llm.backend == "ollama"

    @pytest.mark.asyncio
    async def test_local_llm_left_alone(self):
        """Critical pin (#80 lesson): non-ollama local backends
        like `dual` or `router` MUST NOT be silently reset to
        ollama just because we're hitting the local-defaults code
        path.  The cloud-backend gate is intentionally narrow."""
        from dragon_voice.pipeline_init import build_and_initialize_pipeline
        cfg = _make_conn_config(llm="dual")
        with patch("dragon_voice.pipeline_init.VoicePipeline") as VP:
            VP.return_value.initialize = AsyncMock()
            await build_and_initialize_pipeline(
                _make_ws(),
                ws_id="ws5",
                conn_config=cfg,
                on_audio=AsyncMock(),
                on_event=AsyncMock(),
                conversation=MagicMock(),
                session_id="s",
                media_pipeline=None,
                backend_pool=None,
            )
        # `dual` is local — must survive the reset.
        assert cfg.llm.backend == "dual"

    @pytest.mark.asyncio
    async def test_router_local_backend_left_alone(self):
        """`router` (multi-model fleet, #185) is a local-tier
        backend — must NOT be reset."""
        from dragon_voice.pipeline_init import build_and_initialize_pipeline
        cfg = _make_conn_config(llm="router")
        with patch("dragon_voice.pipeline_init.VoicePipeline") as VP:
            VP.return_value.initialize = AsyncMock()
            await build_and_initialize_pipeline(
                _make_ws(),
                ws_id="ws6",
                conn_config=cfg,
                on_audio=AsyncMock(),
                on_event=AsyncMock(),
                conversation=MagicMock(),
                session_id="s",
                media_pipeline=None,
                backend_pool=None,
            )
        assert cfg.llm.backend == "router"

    @pytest.mark.asyncio
    async def test_system_prompt_and_max_tokens_set_to_local_defaults(self):
        from dragon_voice.config import (
            SYSTEM_PROMPT_LOCAL,
            MAX_TOKENS_LOCAL,
        )
        from dragon_voice.pipeline_init import build_and_initialize_pipeline
        cfg = _make_conn_config()
        with patch("dragon_voice.pipeline_init.VoicePipeline") as VP:
            VP.return_value.initialize = AsyncMock()
            await build_and_initialize_pipeline(
                _make_ws(),
                ws_id="ws7",
                conn_config=cfg,
                on_audio=AsyncMock(),
                on_event=AsyncMock(),
                conversation=MagicMock(),
                session_id="s",
                media_pipeline=None,
                backend_pool=None,
            )
        assert cfg.llm.system_prompt == SYSTEM_PROMPT_LOCAL
        assert cfg.llm.max_tokens == MAX_TOKENS_LOCAL


# ─── Happy path ──────────────────────────────────────────────


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_returns_initialised_pipeline(self):
        from dragon_voice.pipeline_init import build_and_initialize_pipeline
        with patch("dragon_voice.pipeline_init.VoicePipeline") as VP:
            inst = MagicMock()
            inst.initialize = AsyncMock()
            VP.return_value = inst

            result = await build_and_initialize_pipeline(
                _make_ws(),
                ws_id="ws8",
                conn_config=_make_conn_config(),
                on_audio=AsyncMock(),
                on_event=AsyncMock(),
                conversation=MagicMock(),
                session_id="sess",
                media_pipeline=None,
                backend_pool=None,
            )

        assert result is inst
        inst.initialize.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_voicepipeline_constructed_with_tool_callbacks(self):
        """Audit A2 (#142): tool callbacks must reach the pipeline
        constructor so voice turns surface tool indicators."""
        from dragon_voice.pipeline_init import build_and_initialize_pipeline
        on_call = MagicMock()
        on_result = MagicMock()
        on_error = MagicMock()

        with patch("dragon_voice.pipeline_init.VoicePipeline") as VP:
            VP.return_value.initialize = AsyncMock()
            await build_and_initialize_pipeline(
                _make_ws(),
                ws_id="ws9",
                conn_config=_make_conn_config(),
                on_audio=AsyncMock(),
                on_event=AsyncMock(),
                conversation=MagicMock(),
                session_id="sess",
                media_pipeline=None,
                backend_pool=None,
                on_tool_call=on_call,
                on_tool_result=on_result,
                on_tool_error=on_error,
            )

        kwargs = VP.call_args.kwargs
        assert kwargs["on_tool_call"] is on_call
        assert kwargs["on_tool_result"] is on_result
        assert kwargs["on_tool_error"] is on_error

    @pytest.mark.asyncio
    async def test_voicepipeline_constructed_with_surface_mgr(self):
        """Audit B1 (#165): surface_mgr must reach the pipeline so
        scheduler-fired widgets gate behind LLM-token interleave
        protection."""
        from dragon_voice.pipeline_init import build_and_initialize_pipeline
        surface_mgr = MagicMock()

        with patch("dragon_voice.pipeline_init.VoicePipeline") as VP:
            VP.return_value.initialize = AsyncMock()
            await build_and_initialize_pipeline(
                _make_ws(),
                ws_id="ws10",
                conn_config=_make_conn_config(),
                on_audio=AsyncMock(),
                on_event=AsyncMock(),
                conversation=MagicMock(),
                session_id="sess",
                media_pipeline=None,
                backend_pool=None,
                surface_mgr=surface_mgr,
            )

        assert VP.call_args.kwargs["surface_mgr"] is surface_mgr


# ─── Init failure ───────────────────────────────────────────


class TestInitFailure:
    @pytest.mark.asyncio
    async def test_init_failure_emits_pipeline_init_failed_and_returns_none(self):
        from dragon_voice.pipeline_init import build_and_initialize_pipeline
        ws = _make_ws()
        send = _make_safe_send_json()

        with patch("dragon_voice.pipeline_init.VoicePipeline") as VP:
            VP.return_value.initialize = AsyncMock(
                side_effect=RuntimeError("Moonshine OOM"),
            )
            result = await build_and_initialize_pipeline(
                ws,
                ws_id="ws11",
                conn_config=_make_conn_config(),
                on_audio=AsyncMock(),
                on_event=AsyncMock(),
                conversation=MagicMock(),
                session_id="s",
                media_pipeline=None,
                backend_pool=None,
                safe_send_json=send,
            )

        assert result is None
        send.assert_awaited_once()
        frame = send.await_args.args[1]
        assert frame["code"] == "pipeline_init_failed"
        assert frame["severity"] == Severity.FATAL.value
        assert frame["scope"] == Scope.SESSION.value

    @pytest.mark.asyncio
    async def test_error_message_does_not_leak_python_exception_text(self):
        """Pin: the user-facing message MUST NOT be `str(e)`.
        Tab5's caption is 128 chars and we don't want Python
        tracebacks reaching a non-developer user."""
        from dragon_voice.pipeline_init import build_and_initialize_pipeline
        ws = _make_ws()
        send = _make_safe_send_json()

        with patch("dragon_voice.pipeline_init.VoicePipeline") as VP:
            VP.return_value.initialize = AsyncMock(
                side_effect=IndexError("list index out of range"),
            )
            await build_and_initialize_pipeline(
                ws,
                ws_id="ws12",
                conn_config=_make_conn_config(),
                on_audio=AsyncMock(),
                on_event=AsyncMock(),
                conversation=MagicMock(),
                session_id="s",
                media_pipeline=None,
                backend_pool=None,
                safe_send_json=send,
            )

        frame = send.await_args.args[1]
        assert "list index out of range" not in frame["message"]
        assert "Voice pipeline failed to start" in frame["message"]

    @pytest.mark.asyncio
    async def test_init_failure_with_closed_ws_skips_emit(self):
        from dragon_voice.pipeline_init import build_and_initialize_pipeline
        ws = _make_ws(closed=True)
        send = _make_safe_send_json()

        with patch("dragon_voice.pipeline_init.VoicePipeline") as VP:
            VP.return_value.initialize = AsyncMock(
                side_effect=RuntimeError("boom"),
            )
            result = await build_and_initialize_pipeline(
                ws,
                ws_id="ws13",
                conn_config=_make_conn_config(),
                on_audio=AsyncMock(),
                on_event=AsyncMock(),
                conversation=MagicMock(),
                session_id="s",
                media_pipeline=None,
                backend_pool=None,
                safe_send_json=send,
            )

        assert result is None
        send.assert_not_awaited()
        ws.send_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_init_failure_with_no_safe_send_json_uses_raw_ws(self):
        """Backward-compat: callers that haven't wired
        safe_send_json yet still get the error frame via raw
        ws.send_json (preserves pre-extract behaviour)."""
        from dragon_voice.pipeline_init import build_and_initialize_pipeline
        ws = _make_ws()

        with patch("dragon_voice.pipeline_init.VoicePipeline") as VP:
            VP.return_value.initialize = AsyncMock(
                side_effect=RuntimeError("boom"),
            )
            result = await build_and_initialize_pipeline(
                ws,
                ws_id="ws14",
                conn_config=_make_conn_config(),
                on_audio=AsyncMock(),
                on_event=AsyncMock(),
                conversation=MagicMock(),
                session_id="s",
                media_pipeline=None,
                backend_pool=None,
                # no safe_send_json
            )

        assert result is None
        ws.send_json.assert_awaited_once()
        frame = ws.send_json.await_args.args[0]
        assert frame["code"] == "pipeline_init_failed"
