"""Tests for ``dragon_voice.config_update_ack``.

Pin two surfaces:

  * ``_resolve_active_model`` — pure function that picks the
    user-visible model name per vmode + LLM backend.  Five
    branches (CLOUD / TINKERCLAW / OLLAMA / fallback / empty
    config field).
  * ``emit_config_update_ack`` — async ACK builder.  Pin payload
    shape, fleet_summary inclusion contract, ws-closed skip,
    return-value contract.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.config_swap import BackendSelection
from dragon_voice.config_update_ack import (
    _resolve_active_model,
    emit_config_update_ack,
)
from dragon_voice.voice_modes import VoiceMode


def _make_conn_config(
    *,
    openrouter_model: str = "",
    tinkerclaw_model: str = "",
    ollama_model: str = "",
) -> MagicMock:
    cfg = MagicMock()
    cfg.llm.openrouter_model = openrouter_model
    cfg.llm.tinkerclaw_model = tinkerclaw_model
    cfg.llm.ollama_model = ollama_model
    return cfg


def _make_ws(*, closed: bool = False) -> MagicMock:
    ws = MagicMock()
    ws.closed = closed
    return ws


def _make_safe_send_json() -> AsyncMock:
    return AsyncMock(return_value=True)


def _make_conversation_with_summary(summary):
    """Build a ConversationEngine stub whose fleet_summary returns
    the given dict (or None for non-router backends)."""
    conv = MagicMock()
    conv.fleet_summary = MagicMock(return_value=summary)
    return conv


def _backends(*, llm: str = "ollama") -> BackendSelection:
    return BackendSelection(
        stt_backend="moonshine",
        tts_backend="piper",
        llm_backend=llm,
    )


# ─── _resolve_active_model — pure function ────────────────────────


class TestResolveActiveModel:
    def test_cloud_returns_openrouter_model(self):
        cfg = _make_conn_config(openrouter_model="anthropic/claude-sonnet-4.6")
        out = _resolve_active_model(VoiceMode.CLOUD, cfg, "openrouter")
        assert out == "anthropic/claude-sonnet-4.6"

    def test_tinkerclaw_returns_tinkerclaw_model(self):
        cfg = _make_conn_config(tinkerclaw_model="minimax/MiniMax-M2.5")
        out = _resolve_active_model(VoiceMode.TINKERCLAW, cfg, "tinkerclaw")
        assert out == "minimax/MiniMax-M2.5"

    def test_local_with_ollama_returns_ollama_model(self):
        cfg = _make_conn_config(ollama_model="ministral-3:3b")
        out = _resolve_active_model(VoiceMode.LOCAL, cfg, "ollama")
        assert out == "ministral-3:3b"

    def test_other_backend_returns_empty(self):
        """Non-ollama local backends (npu_genie, dual, lmstudio) drop
        through to "" because the ACK has no field for them yet."""
        cfg = _make_conn_config()
        out = _resolve_active_model(VoiceMode.LOCAL, cfg, "npu_genie")
        assert out == ""

    def test_missing_model_field_returns_empty_string_not_none(self):
        """Empty config field → "" (not None) so the ACK payload
        always serializes the field as a string."""
        cfg = _make_conn_config(openrouter_model="")
        out = _resolve_active_model(VoiceMode.CLOUD, cfg, "openrouter")
        assert out == ""


# ─── emit_config_update_ack — async ACK builder ──────────────────


class TestEmitConfigUpdateAck:
    @pytest.mark.asyncio
    async def test_local_payload_shape(self):
        ws = _make_ws()
        cfg = _make_conn_config(ollama_model="ministral-3:3b")
        send = _make_safe_send_json()

        out = await emit_config_update_ack(
            ws,
            vmode=VoiceMode.LOCAL,
            conn_config=cfg,
            backends=_backends(llm="ollama"),
            conversation=None,
            safe_send_json=send,
        )

        assert out == "ministral-3:3b"
        send.assert_awaited_once()
        envelope = send.await_args.args[1]
        assert envelope["type"] == "config_update"
        cfg_payload = envelope["config"]
        assert cfg_payload == {
            "stt": "moonshine",
            "tts": "piper",
            "llm": "ollama",
            "llm_model": "ministral-3:3b",
            "voice_mode": 0,
            "cloud_mode": False,
        }
        # No fleet_summary key when conversation is None.
        assert "fleet_summary" not in cfg_payload

    @pytest.mark.asyncio
    async def test_cloud_payload_has_cloud_mode_true(self):
        ws = _make_ws()
        cfg = _make_conn_config(openrouter_model="anthropic/claude-opus-4.7")
        send = _make_safe_send_json()

        await emit_config_update_ack(
            ws,
            vmode=VoiceMode.CLOUD,
            conn_config=cfg,
            backends=BackendSelection("openrouter", "openrouter", "openrouter"),
            conversation=None,
            safe_send_json=send,
        )
        cfg_payload = send.await_args.args[1]["config"]
        assert cfg_payload["voice_mode"] == 2
        assert cfg_payload["cloud_mode"] is True
        assert cfg_payload["llm_model"] == "anthropic/claude-opus-4.7"

    @pytest.mark.asyncio
    async def test_local_payload_has_cloud_mode_false(self):
        ws = _make_ws()
        cfg = _make_conn_config(ollama_model="ministral-3:3b")
        send = _make_safe_send_json()

        await emit_config_update_ack(
            ws,
            vmode=VoiceMode.LOCAL,
            conn_config=cfg,
            backends=_backends(llm="ollama"),
            conversation=None,
            safe_send_json=send,
        )
        cfg_payload = send.await_args.args[1]["config"]
        assert cfg_payload["cloud_mode"] is False

    @pytest.mark.asyncio
    async def test_router_active_includes_fleet_summary(self):
        """When ConversationEngine.fleet_summary returns a dict (i.e.
        the active LLM is a CapabilityAwareRouter), it MUST land in
        the payload so Tab5 can render its capability chips."""
        ws = _make_ws()
        cfg = _make_conn_config(openrouter_model="qwen/qwen3.6-flash")
        send = _make_safe_send_json()
        fleet = {
            "text":     "ministral-3:3b",
            "vision":   "qwen/qwen3.6-flash",
            "video":    None,
            "audio_in": None,
            "audio_out": None,
            "tool_calling": "ministral-3:3b",
        }
        conv = _make_conversation_with_summary(fleet)

        await emit_config_update_ack(
            ws,
            vmode=VoiceMode.CLOUD,
            conn_config=cfg,
            backends=BackendSelection("openrouter", "openrouter", "openrouter"),
            conversation=conv,
            safe_send_json=send,
        )

        cfg_payload = send.await_args.args[1]["config"]
        assert cfg_payload["fleet_summary"] == fleet
        # fleet_summary asked with the right voice_mode int
        conv.fleet_summary.assert_called_once_with(2)

    @pytest.mark.asyncio
    async def test_non_router_omits_fleet_summary_field(self):
        """Single-backend configurations return None from
        fleet_summary; the field MUST be omitted from the payload
        (not present-with-null) so Tab5's old firmware that relies
        on missing-field semantics keeps working."""
        ws = _make_ws()
        cfg = _make_conn_config(ollama_model="ministral-3:3b")
        send = _make_safe_send_json()
        conv = _make_conversation_with_summary(None)  # non-router

        await emit_config_update_ack(
            ws,
            vmode=VoiceMode.LOCAL,
            conn_config=cfg,
            backends=_backends(llm="ollama"),
            conversation=conv,
            safe_send_json=send,
        )

        cfg_payload = send.await_args.args[1]["config"]
        assert "fleet_summary" not in cfg_payload

    @pytest.mark.asyncio
    async def test_ws_closed_skips_send_but_returns_active_model(self):
        """If the WS is already closed (client reconnecting),
        skip the send but still return the active_model so
        downstream callers (vision_capability emit) get a real
        string and don't fall through to ""."""
        ws = _make_ws(closed=True)
        cfg = _make_conn_config(openrouter_model="anthropic/claude-haiku-4.5")
        send = _make_safe_send_json()

        out = await emit_config_update_ack(
            ws,
            vmode=VoiceMode.CLOUD,
            conn_config=cfg,
            backends=BackendSelection("openrouter", "openrouter", "openrouter"),
            conversation=None,
            safe_send_json=send,
        )

        assert out == "anthropic/claude-haiku-4.5"
        send.assert_not_awaited()
