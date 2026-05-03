"""Tests for ``dragon_voice.text_path_receipt``.

Two functions, two test classes:

  * ``TestEmitTextPathLlmReceipt`` — local-path SupportsUsage
    receipt.  Pin the no-op branches (None conv, no _llm,
    non-SupportsUsage backend, empty usage) and the happy path
    (full receipt frame with cost_mils + retried).

  * ``TestEmitTinkerClawZeroCostReceipt`` — TC receipt.  Pin the
    model-name resolution priority chain (4 fallback levels) and
    the zero-cost frame shape.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.text_path_receipt import (
    emit_text_path_llm_receipt,
    emit_tinkerclaw_zero_cost_receipt,
)


def _make_ws(*, closed: bool = False) -> MagicMock:
    ws = MagicMock()
    ws.closed = closed
    return ws


def _make_safe_send_json() -> AsyncMock:
    return AsyncMock(return_value=True)


# ─── emit_text_path_llm_receipt ─────────────────────────────────


class TestEmitTextPathLlmReceipt:
    @pytest.mark.asyncio
    async def test_none_conversation_is_noop(self):
        ws = _make_ws()
        send = _make_safe_send_json()

        await emit_text_path_llm_receipt(
            ws,
            conversation=None,
            safe_send_json=send,
        )
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_llm_attached_is_noop(self):
        """During boot or post-shutdown, ConvEngine may have no
        active LLM.  Pin the silent skip."""
        ws = _make_ws()
        send = _make_safe_send_json()
        conv = MagicMock(spec=[])  # no _llm attr at all
        # MagicMock returns another mock by default; force None
        # via spec=[]; getattr(conv, "_llm", None) returns None.

        await emit_text_path_llm_receipt(
            ws, conversation=conv, safe_send_json=send,
        )
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_supports_usage_backend_skips(self):
        """A backend that doesn't implement SupportsUsage (e.g.
        plain TinkerClaw, dual-model picker) MUST be silently
        skipped — they have a different receipt path."""
        ws = _make_ws()
        send = _make_safe_send_json()
        # Plain backend with no get_last_usage method.
        plain_backend = MagicMock(spec=[])
        conv = MagicMock()
        conv._llm = plain_backend

        await emit_text_path_llm_receipt(
            ws, conversation=conv, safe_send_json=send,
        )
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_usage_skips(self):
        """If get_last_usage returns empty / no total_tokens,
        skip — Wave 21b silent-fallback closure."""
        from dragon_voice.llm.base import LLMBackend, SupportsUsage

        # Real subclass that satisfies SupportsUsage protocol
        class _Backend:
            def get_last_usage(self):
                return {}  # empty
        ws = _make_ws()
        send = _make_safe_send_json()
        conv = MagicMock()
        conv._llm = _Backend()
        assert isinstance(_Backend(), SupportsUsage)

        await emit_text_path_llm_receipt(
            ws, conversation=conv, safe_send_json=send,
        )
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_zero_total_tokens_skips(self):
        """A usage dict with total_tokens=0 means the LLM emitted
        no real content (cancel? error?).  Skip the receipt so
        the chat bubble doesn't get a useless 0-cost stamp."""
        class _Backend:
            def get_last_usage(self):
                return {
                    "model": "anthropic/claude-haiku-4.5",
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                }
        ws = _make_ws()
        send = _make_safe_send_json()
        conv = MagicMock()
        conv._llm = _Backend()

        await emit_text_path_llm_receipt(
            ws, conversation=conv, safe_send_json=send,
        )
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_happy_path_emits_full_receipt(self):
        class _Backend:
            def get_last_usage(self):
                return {
                    "model": "anthropic/claude-haiku-4.5",
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                }
        ws = _make_ws()
        send = _make_safe_send_json()
        conv = MagicMock()
        conv._llm = _Backend()

        await emit_text_path_llm_receipt(
            ws, conversation=conv, safe_send_json=send,
        )

        send.assert_awaited_once()
        payload = send.await_args.args[1]
        assert payload["type"] == "receipt"
        assert payload["stage"] == "llm"
        assert payload["model"] == "anthropic/claude-haiku-4.5"
        assert payload["prompt_tokens"] == 100
        assert payload["completion_tokens"] == 50
        assert payload["total_tokens"] == 150
        # Cost computed via price_for_model — non-zero for haiku
        assert payload["cost_mils"] > 0
        assert payload["retried"] is False  # default
        assert payload["retry_reason"] == ""

    @pytest.mark.asyncio
    async def test_retried_field_surfaced(self):
        """v4·D Gauntlet G2: when usage carries retried=True
        (context_trim or 429 backoff), the receipt MUST surface
        it so Tab5 can render a 'RETRIED' chip."""
        class _Backend:
            def get_last_usage(self):
                return {
                    "model": "anthropic/claude-sonnet-4.6",
                    "prompt_tokens": 200,
                    "completion_tokens": 100,
                    "total_tokens": 300,
                    "retried": True,
                    "retry_reason": "context_trim",
                }
        ws = _make_ws()
        send = _make_safe_send_json()
        conv = MagicMock()
        conv._llm = _Backend()

        await emit_text_path_llm_receipt(
            ws, conversation=conv, safe_send_json=send,
        )
        payload = send.await_args.args[1]
        assert payload["retried"] is True
        assert payload["retry_reason"] == "context_trim"

    @pytest.mark.asyncio
    async def test_ws_closed_skips_send(self):
        class _Backend:
            def get_last_usage(self):
                return {"model": "x", "total_tokens": 100, "prompt_tokens": 50, "completion_tokens": 50}
        ws = _make_ws(closed=True)
        send = _make_safe_send_json()
        conv = MagicMock()
        conv._llm = _Backend()

        await emit_text_path_llm_receipt(
            ws, conversation=conv, safe_send_json=send,
        )
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exception_in_usage_is_logged_not_raised(self):
        """If get_last_usage raises (some backend bug), log +
        swallow.  Receipt is informational."""
        class _Backend:
            def get_last_usage(self):
                raise RuntimeError("usage tracker corrupt")
        ws = _make_ws()
        send = _make_safe_send_json()
        conv = MagicMock()
        conv._llm = _Backend()

        # Must NOT raise.
        await emit_text_path_llm_receipt(
            ws, conversation=conv, safe_send_json=send,
        )


# ─── emit_tinkerclaw_zero_cost_receipt ───────────────────────────


class TestEmitTinkerClawZeroCostReceipt:
    @pytest.mark.asyncio
    async def test_picks_inner_model_first(self):
        """Priority 1: llm._model takes precedence over name and
        config default."""
        ws = _make_ws()
        send = _make_safe_send_json()
        llm = MagicMock()
        llm._model = "loaded-model-A"
        llm.name = "should-not-use-this"

        await emit_tinkerclaw_zero_cost_receipt(
            ws, llm=llm, tinkerclaw_model_default="default-model",
            safe_send_json=send,
        )
        payload = send.await_args.args[1]
        assert payload["model"] == "loaded-model-A"

    @pytest.mark.asyncio
    async def test_falls_back_to_name_when_inner_empty(self):
        ws = _make_ws()
        send = _make_safe_send_json()
        llm = MagicMock()
        llm._model = ""
        llm.name = "name-fallback"

        await emit_tinkerclaw_zero_cost_receipt(
            ws, llm=llm, tinkerclaw_model_default="default-model",
            safe_send_json=send,
        )
        payload = send.await_args.args[1]
        assert payload["model"] == "name-fallback"

    @pytest.mark.asyncio
    async def test_falls_back_to_default_when_name_empty(self):
        ws = _make_ws()
        send = _make_safe_send_json()
        llm = MagicMock()
        llm._model = ""
        llm.name = ""

        await emit_tinkerclaw_zero_cost_receipt(
            ws, llm=llm, tinkerclaw_model_default="config-default",
            safe_send_json=send,
        )
        payload = send.await_args.args[1]
        assert payload["model"] == "config-default"

    @pytest.mark.asyncio
    async def test_falls_back_to_minimax_last_resort(self):
        """When EVERY field is empty, last-resort fallback to
        minimax/MiniMax-M2.5 keeps the bubble stamp from being
        the bare 'tinkerclaw' string (Wave 8 audit #2 fix)."""
        ws = _make_ws()
        send = _make_safe_send_json()
        llm = MagicMock()
        llm._model = ""
        llm.name = ""

        await emit_tinkerclaw_zero_cost_receipt(
            ws, llm=llm, tinkerclaw_model_default="",
            safe_send_json=send,
        )
        payload = send.await_args.args[1]
        assert payload["model"] == "minimax/MiniMax-M2.5"

    @pytest.mark.asyncio
    async def test_zero_cost_frame_shape(self):
        """The TC receipt MUST always carry zeros for token counts
        + cost (TC bills to its own gateway).  Pin the wire shape
        so a future change can't accidentally surface phantom
        cost from the WS payload."""
        ws = _make_ws()
        send = _make_safe_send_json()
        llm = MagicMock()
        llm._model = "minimax/MiniMax-M2.5"

        await emit_tinkerclaw_zero_cost_receipt(
            ws, llm=llm, tinkerclaw_model_default="",
            safe_send_json=send,
        )
        payload = send.await_args.args[1]
        assert payload["type"] == "receipt"
        assert payload["stage"] == "llm"
        assert payload["prompt_tokens"] == 0
        assert payload["completion_tokens"] == 0
        assert payload["total_tokens"] == 0
        assert payload["cost_mils"] == 0
        assert payload["llm_ms"] == 0
        assert payload["retried"] is False
        assert payload["retry_reason"] == ""

    @pytest.mark.asyncio
    async def test_ws_closed_skips_send(self):
        ws = _make_ws(closed=True)
        send = _make_safe_send_json()
        llm = MagicMock()
        llm._model = "anything"

        await emit_tinkerclaw_zero_cost_receipt(
            ws, llm=llm, tinkerclaw_model_default="",
            safe_send_json=send,
        )
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_failure_swallowed_at_debug_level(self):
        """TC receipt failure logs at DEBUG (purely informational
        path) — pin so an ops watcher doesn't get noise from
        every TC turn when a transport hiccups."""
        ws = _make_ws()
        send = AsyncMock(side_effect=ConnectionResetError("dead"))
        llm = MagicMock()
        llm._model = "x"

        # Must NOT raise.
        await emit_tinkerclaw_zero_cost_receipt(
            ws, llm=llm, tinkerclaw_model_default="",
            safe_send_json=send,
        )
