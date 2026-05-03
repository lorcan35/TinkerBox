"""Tests for ``dragon_voice.session_handshake``.

Two surfaces:

  * ``emit_session_start`` — async sender for the post-register
    handshake frame.  Pin payload shape, fleet_summary inclusion
    contract, drop-on-send-fail return semantics.

  * ``replay_session_message_tail`` — async optional replay of
    the message tail on session resume.  Pin no-op branches
    (no message_store, empty store, all-skipped messages) and
    failure-isolation (store raises → logged + swallowed).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.session_handshake import (
    emit_session_start,
    replay_session_message_tail,
)


def _make_ws() -> MagicMock:
    ws = MagicMock()
    return ws


def _make_safe_send_json(returns: bool = True) -> AsyncMock:
    return AsyncMock(return_value=returns)


def _make_conn_config(
    *,
    stt: str = "moonshine",
    tts: str = "piper",
    llm: str = "ollama",
    sample_rate: int = 16000,
    system_prompt: str = "(local)",
) -> MagicMock:
    cfg = MagicMock()
    cfg.stt.backend = stt
    cfg.tts.backend = tts
    cfg.llm.backend = llm
    cfg.audio.input_sample_rate = sample_rate
    cfg.llm.system_prompt = system_prompt
    return cfg


def _make_conv_with_summary(summary):
    conv = MagicMock()
    conv.fleet_summary = MagicMock(return_value=summary)
    return conv


# ─── emit_session_start ──────────────────────────────────────────


class TestEmitSessionStart:
    @pytest.mark.asyncio
    async def test_happy_path_returns_true_with_full_payload(self):
        ws = _make_ws()
        cfg = _make_conn_config()
        send = _make_safe_send_json(returns=True)

        out = await emit_session_start(
            ws,
            session_id="sess-XYZ",
            device_id="dev-A",
            resumed=False,
            message_count=0,
            ws_id="ws-1",
            conn_config=cfg,
            conversation=None,
            voice_mode=0,
            safe_send_json=send,
        )

        assert out is True
        send.assert_awaited_once()
        envelope = send.await_args.args[1]
        assert envelope["type"] == "session_start"
        assert envelope["session_id"] == "sess-XYZ"
        assert envelope["device_id"] == "dev-A"
        assert envelope["resumed"] is False
        assert envelope["message_count"] == 0

        cfg_payload = envelope["config"]
        assert cfg_payload == {
            "stt": "moonshine",
            "tts": "piper",
            "llm": "ollama",
            "tts_sample_rate": 16000,
            "response_mode": "match_input",
            "system_prompt": "(local)",
        }
        # No fleet_summary key when conversation is None.
        assert "fleet_summary" not in cfg_payload

    @pytest.mark.asyncio
    async def test_resumed_session_carries_message_count(self):
        ws = _make_ws()
        send = _make_safe_send_json()

        await emit_session_start(
            ws,
            session_id="sess-XYZ",
            device_id="dev-A",
            resumed=True,
            message_count=42,
            ws_id="ws-1",
            conn_config=_make_conn_config(),
            conversation=None,
            voice_mode=0,
            safe_send_json=send,
        )

        envelope = send.await_args.args[1]
        assert envelope["resumed"] is True
        assert envelope["message_count"] == 42

    @pytest.mark.asyncio
    async def test_router_active_includes_fleet_summary(self):
        """When ConversationEngine.fleet_summary returns a dict
        (CapabilityAwareRouter active), it MUST land in the
        config payload so Tab5 can render its capability chips
        without a fresh config_update."""
        ws = _make_ws()
        send = _make_safe_send_json()
        fleet = {
            "text":     "ministral-3:3b",
            "vision":   "qwen/qwen3.6-flash",
            "video":    None,
            "audio_in": None,
            "audio_out": None,
            "tool_calling": "ministral-3:3b",
        }
        conv = _make_conv_with_summary(fleet)

        await emit_session_start(
            ws,
            session_id="sess-XYZ",
            device_id="dev-A",
            resumed=False,
            message_count=0,
            ws_id="ws-1",
            conn_config=_make_conn_config(),
            conversation=conv,
            voice_mode=2,
            safe_send_json=send,
        )

        cfg_payload = send.await_args.args[1]["config"]
        assert cfg_payload["fleet_summary"] == fleet
        # Asked with the right voice_mode int
        conv.fleet_summary.assert_called_once_with(2)

    @pytest.mark.asyncio
    async def test_non_router_omits_fleet_summary_field(self):
        """Single-backend configurations return None from
        fleet_summary; the field MUST be omitted (not present-
        with-null) so Tab5 firmware that uses missing-field
        semantics keeps working."""
        ws = _make_ws()
        send = _make_safe_send_json()
        conv = _make_conv_with_summary(None)

        await emit_session_start(
            ws,
            session_id="sess-XYZ",
            device_id="dev-A",
            resumed=False,
            message_count=0,
            ws_id="ws-1",
            conn_config=_make_conn_config(),
            conversation=conv,
            voice_mode=0,
            safe_send_json=send,
        )
        cfg_payload = send.await_args.args[1]["config"]
        assert "fleet_summary" not in cfg_payload

    @pytest.mark.asyncio
    async def test_send_drop_returns_false(self):
        """If safe_send_json reports drop (transport half-closed),
        return False so the caller short-circuits — Tab5 will
        reconnect and we'll replay session_start fresh."""
        ws = _make_ws()
        send = _make_safe_send_json(returns=False)

        out = await emit_session_start(
            ws,
            session_id="sess-XYZ",
            device_id="dev-A",
            resumed=False,
            message_count=0,
            ws_id="ws-1",
            conn_config=_make_conn_config(),
            conversation=None,
            voice_mode=0,
            safe_send_json=send,
        )
        assert out is False


# ─── replay_session_message_tail ─────────────────────────────────


class TestReplaySessionMessageTail:
    @pytest.mark.asyncio
    async def test_no_message_store_is_noop(self):
        """During boot or test paths, message_store may be None."""
        ws = _make_ws()
        send = _make_safe_send_json()

        await replay_session_message_tail(
            ws,
            session_id="sess-X",
            ws_id="ws-1",
            message_store=None,
            safe_send_json=send,
        )

        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_store_skips_send(self):
        ws = _make_ws()
        send = _make_safe_send_json()
        store = MagicMock()
        store.get_messages = AsyncMock(return_value=[])

        await replay_session_message_tail(
            ws,
            session_id="sess-X",
            ws_id="ws-1",
            message_store=store,
            safe_send_json=send,
        )

        store.get_messages.assert_awaited_once()
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_happy_path_sends_session_messages_envelope(self):
        ws = _make_ws()
        send = _make_safe_send_json()
        store = MagicMock()
        store.get_messages = AsyncMock(return_value=[
            {"role": "user", "content": "hello", "created_at": "2026-05-01T12:00"},
            {"role": "assistant", "content": "hi there", "created_at": "2026-05-01T12:01"},
        ])

        await replay_session_message_tail(
            ws,
            session_id="sess-XYZ",
            ws_id="ws-1",
            message_store=store,
            safe_send_json=send,
        )

        send.assert_awaited_once()
        envelope = send.await_args.args[1]
        assert envelope["type"] == "session_messages"
        assert envelope["session_id"] == "sess-XYZ"
        assert len(envelope["items"]) == 2
        assert envelope["items"][0] == {
            "role": "user",
            "content": "hello",
            "timestamp": "2026-05-01T12:00",
        }

    @pytest.mark.asyncio
    async def test_messages_with_missing_role_or_content_skipped(self):
        """Defensive: pre-extract code skipped messages with
        empty role/content (legacy tool-result frames etc.).
        Pin that filter."""
        ws = _make_ws()
        send = _make_safe_send_json()
        store = MagicMock()
        store.get_messages = AsyncMock(return_value=[
            {"role": "user", "content": "valid"},
            {"role": "", "content": "no role"},          # skipped
            {"role": "assistant", "content": ""},         # skipped
            {"role": None, "content": "still no role"},   # skipped
            {"role": "user", "content": "also valid"},
        ])

        await replay_session_message_tail(
            ws,
            session_id="sess-X",
            ws_id="ws-1",
            message_store=store,
            safe_send_json=send,
        )

        envelope = send.await_args.args[1]
        assert len(envelope["items"]) == 2
        contents = [i["content"] for i in envelope["items"]]
        assert contents == ["valid", "also valid"]

    @pytest.mark.asyncio
    async def test_all_messages_skipped_no_send(self):
        """If every message is filtered out, don't send an empty
        session_messages envelope."""
        ws = _make_ws()
        send = _make_safe_send_json()
        store = MagicMock()
        store.get_messages = AsyncMock(return_value=[
            {"role": "", "content": "x"},
            {"role": None, "content": "y"},
        ])

        await replay_session_message_tail(
            ws,
            session_id="sess-X",
            ws_id="ws-1",
            message_store=store,
            safe_send_json=send,
        )
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_store_exception_logged_not_raised(self):
        """If the store raises (DB down, schema migration in
        progress), log + swallow.  Replay is a nice-to-have."""
        ws = _make_ws()
        send = _make_safe_send_json()
        store = MagicMock()
        store.get_messages = AsyncMock(side_effect=RuntimeError("DB busy"))

        # Must NOT raise.
        await replay_session_message_tail(
            ws,
            session_id="sess-X",
            ws_id="ws-1",
            message_store=store,
            safe_send_json=send,
        )

    @pytest.mark.asyncio
    async def test_long_history_truncated_to_limit(self):
        """A 100-message history should be capped to the most-recent 20."""
        ws = _make_ws()
        send = _make_safe_send_json()
        store = MagicMock()
        msgs = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg-{i}"}
            for i in range(100)
        ]
        # Note: get_messages is itself called with limit=20, so the
        # limit truncation happens at the DB layer.  The post-call
        # tail-slice is defence-in-depth.  We mock get_messages to
        # return ALL 100 to exercise the slice.
        store.get_messages = AsyncMock(return_value=msgs)

        await replay_session_message_tail(
            ws,
            session_id="sess-X",
            ws_id="ws-1",
            message_store=store,
            safe_send_json=send,
            limit=20,
        )

        envelope = send.await_args.args[1]
        # Last 20 of msg-0..msg-99 = msg-80..msg-99
        assert len(envelope["items"]) == 20
        assert envelope["items"][0]["content"] == "msg-80"
        assert envelope["items"][-1]["content"] == "msg-99"
