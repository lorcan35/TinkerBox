"""Tests for ``dragon_voice.config_finalize``.

Pin two surfaces:

  * ``persist_session_config_to_db`` — async DB write with failure
    isolation.  6 branches: happy path, no-db, no-session, DB
    raises, active_model derivation per backend, llm_model truncation
    at 128 chars.

  * ``apply_swap_config_to_conn`` — sync conn_config mutation.
    7 branches: backend names always stamped; API key propagation
    for HYBRID/CLOUD/TC-with-cloud-suffix; NO propagation for
    LOCAL/TC-default.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.config_finalize import (
    _resolve_active_model_for_db,
    apply_swap_config_to_conn,
    persist_session_config_to_db,
)
from dragon_voice.voice_modes import VoiceMode


def _make_conn_config(
    *,
    openrouter_model: str = "",
    tinkerclaw_model: str = "",
    ollama_model: str = "",
    openrouter_api_key: str = "sk-test",
    openrouter_url: str = "https://openrouter.ai/api/v1",
    system_prompt: str = "(initial)",
) -> MagicMock:
    cfg = MagicMock()
    cfg.llm.openrouter_model = openrouter_model
    cfg.llm.tinkerclaw_model = tinkerclaw_model
    cfg.llm.ollama_model = ollama_model
    cfg.llm.openrouter_api_key = openrouter_api_key
    cfg.llm.openrouter_url = openrouter_url
    cfg.llm.system_prompt = system_prompt
    return cfg


# ─── _resolve_active_model_for_db (pure function) ────────────────


class TestResolveActiveModelForDb:
    def test_cloud_returns_openrouter_model(self):
        cfg = _make_conn_config(openrouter_model="anthropic/claude-sonnet-4.6")
        out = _resolve_active_model_for_db(
            VoiceMode.CLOUD, cfg, "openrouter", llm_model_request="ignored",
        )
        assert out == "anthropic/claude-sonnet-4.6"

    def test_tinkerclaw_returns_tinkerclaw_model(self):
        cfg = _make_conn_config(tinkerclaw_model="minimax/MiniMax-M2.5")
        out = _resolve_active_model_for_db(
            VoiceMode.TINKERCLAW, cfg, "tinkerclaw", llm_model_request=None,
        )
        assert out == "minimax/MiniMax-M2.5"

    def test_local_with_ollama_returns_ollama_model(self):
        cfg = _make_conn_config(ollama_model="ministral-3:3b")
        out = _resolve_active_model_for_db(
            VoiceMode.LOCAL, cfg, "ollama", llm_model_request=None,
        )
        assert out == "ministral-3:3b"

    def test_other_backend_falls_back_to_request(self):
        """Pinned divergence from config_update_ack._resolve_active_model:
        the DB persist path falls back to the user's requested
        llm_model when the chosen backend isn't openrouter / TC /
        ollama (matching pre-extract behaviour at server.py:2218).
        """
        cfg = _make_conn_config()
        out = _resolve_active_model_for_db(
            VoiceMode.LOCAL, cfg, "npu_genie", llm_model_request="custom-id",
        )
        assert out == "custom-id"

    def test_other_backend_with_no_request_returns_empty(self):
        cfg = _make_conn_config()
        out = _resolve_active_model_for_db(
            VoiceMode.LOCAL, cfg, "npu_genie", llm_model_request=None,
        )
        assert out == ""


# ─── persist_session_config_to_db (async DB write) ───────────────


class TestPersistSessionConfigToDb:
    @pytest.mark.asyncio
    async def test_happy_path_writes_session_row(self):
        db = MagicMock()
        db.update_session = AsyncMock()
        cfg = _make_conn_config(
            ollama_model="ministral-3:3b",
            system_prompt="(local prompt)",
        )

        await persist_session_config_to_db(
            db,
            session_id="sess-XYZ",
            vmode=VoiceMode.LOCAL,
            conn_config=cfg,
            llm_backend="ollama",
            llm_model_request="ministral-3:3b",
        )

        db.update_session.assert_awaited_once_with(
            "sess-XYZ",
            system_prompt="(local prompt)",
            voice_mode=0,
            llm_model="ministral-3:3b",
        )

    @pytest.mark.asyncio
    async def test_no_db_is_noop(self):
        """Test paths or embedded usage may not have a DB wired."""
        await persist_session_config_to_db(
            None,
            session_id="sess-X",
            vmode=VoiceMode.LOCAL,
            conn_config=_make_conn_config(),
            llm_backend="ollama",
            llm_model_request=None,
        )
        # no exception, no call

    @pytest.mark.asyncio
    async def test_no_session_id_is_noop(self):
        """Boot race — no session has been created yet."""
        db = MagicMock()
        db.update_session = AsyncMock()

        for empty in (None, ""):
            await persist_session_config_to_db(
                db,
                session_id=empty,
                vmode=VoiceMode.LOCAL,
                conn_config=_make_conn_config(),
                llm_backend="ollama",
                llm_model_request=None,
            )

        db.update_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_db_exception_is_logged_not_raised(self):
        """A failed DB write must NOT block the in-memory pipeline
        swap — the in-memory state IS the runtime source of truth."""
        db = MagicMock()
        db.update_session = AsyncMock(side_effect=RuntimeError("DB down"))

        # Must NOT raise.
        await persist_session_config_to_db(
            db,
            session_id="sess-X",
            vmode=VoiceMode.LOCAL,
            conn_config=_make_conn_config(),
            llm_backend="ollama",
            llm_model_request=None,
        )

    @pytest.mark.asyncio
    async def test_long_llm_model_truncated_to_128(self):
        """Pin the [:128] guard — the DB column is bounded."""
        db = MagicMock()
        db.update_session = AsyncMock()
        long_id = "a" * 200  # 200 chars
        cfg = _make_conn_config(openrouter_model=long_id)

        await persist_session_config_to_db(
            db,
            session_id="sess-X",
            vmode=VoiceMode.CLOUD,
            conn_config=cfg,
            llm_backend="openrouter",
            llm_model_request=None,
        )

        kwargs = db.update_session.await_args.kwargs
        assert len(kwargs["llm_model"]) == 128
        assert kwargs["llm_model"] == "a" * 128


# ─── apply_swap_config_to_conn (sync conn_config mutation) ───────


class TestApplySwapConfigToConn:
    def test_stamps_backend_names(self):
        cfg = _make_conn_config()
        cfg.stt.backend = "(was)"
        cfg.tts.backend = "(was)"
        cfg.llm.backend = "(was)"

        apply_swap_config_to_conn(
            cfg,
            vmode=VoiceMode.LOCAL,
            stt_backend="moonshine",
            tts_backend="piper",
            llm_backend="ollama",
        )

        assert cfg.stt.backend == "moonshine"
        assert cfg.tts.backend == "piper"
        assert cfg.llm.backend == "ollama"

    def test_local_does_not_propagate_api_keys(self):
        cfg = _make_conn_config(openrouter_api_key="sk-test", openrouter_url="https://X")
        # Pre-call: STT/TTS subconfigs have their own (empty) keys.
        cfg.stt.openrouter_api_key = ""
        cfg.tts.openrouter_api_key = ""

        apply_swap_config_to_conn(
            cfg,
            vmode=VoiceMode.LOCAL,
            stt_backend="moonshine",
            tts_backend="piper",
            llm_backend="ollama",
        )

        # No propagation in LOCAL mode.
        assert cfg.stt.openrouter_api_key == ""
        assert cfg.tts.openrouter_api_key == ""

    def test_hybrid_propagates_api_keys(self):
        cfg = _make_conn_config(openrouter_api_key="sk-test", openrouter_url="https://X")
        cfg.stt.openrouter_api_key = ""
        cfg.tts.openrouter_api_key = ""

        apply_swap_config_to_conn(
            cfg,
            vmode=VoiceMode.HYBRID,
            stt_backend="openrouter",
            tts_backend="openrouter",
            llm_backend="ollama",
        )

        assert cfg.stt.openrouter_api_key == "sk-test"
        assert cfg.stt.openrouter_url == "https://X"
        assert cfg.tts.openrouter_api_key == "sk-test"
        assert cfg.tts.openrouter_url == "https://X"

    def test_cloud_propagates_api_keys(self):
        cfg = _make_conn_config(openrouter_api_key="sk-test", openrouter_url="https://X")

        apply_swap_config_to_conn(
            cfg,
            vmode=VoiceMode.CLOUD,
            stt_backend="openrouter",
            tts_backend="openrouter",
            llm_backend="openrouter",
        )

        assert cfg.stt.openrouter_api_key == "sk-test"
        assert cfg.tts.openrouter_api_key == "sk-test"

    def test_tc_with_cloud_stt_propagates_api_keys(self):
        """TinkerClaw mode that opted into cloud STT/TTS via the
        'cloud' suffix needs the API keys propagated even though
        vmode.needs_cloud_stt_tts() is False for TC."""
        cfg = _make_conn_config(openrouter_api_key="sk-tc", openrouter_url="https://X")

        apply_swap_config_to_conn(
            cfg,
            vmode=VoiceMode.TINKERCLAW,
            stt_backend="openrouter",  # cloud-suffix override
            tts_backend="openrouter",
            llm_backend="tinkerclaw",
        )

        assert cfg.stt.openrouter_api_key == "sk-tc"
        assert cfg.tts.openrouter_api_key == "sk-tc"

    def test_tc_with_local_stt_does_not_propagate(self):
        """TinkerClaw default (local STT/TTS) doesn't need the
        OpenRouter API key for STT/TTS subconfigs."""
        cfg = _make_conn_config(openrouter_api_key="sk-tc", openrouter_url="https://X")
        cfg.stt.openrouter_api_key = ""

        apply_swap_config_to_conn(
            cfg,
            vmode=VoiceMode.TINKERCLAW,
            stt_backend="moonshine",  # local default
            tts_backend="piper",
            llm_backend="tinkerclaw",
        )

        # No propagation when STT is local.
        assert cfg.stt.openrouter_api_key == ""

    def test_returns_none(self):
        """Pure mutation; no return value."""
        cfg = _make_conn_config()
        out = apply_swap_config_to_conn(
            cfg,
            vmode=VoiceMode.LOCAL,
            stt_backend="moonshine",
            tts_backend="piper",
            llm_backend="ollama",
        )
        assert out is None
