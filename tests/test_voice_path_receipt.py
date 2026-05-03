"""Tests for ``dragon_voice.voice_path_receipt``.

Pin every receipt-emit branch + the v4·D audit P0 fallback +
the SupportsUsage gate (Wave 21b #204) so a future refactor
can't regress the receipt shape that Tab5 chat bubbles +
day-budget accumulator depend on.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dragon_voice.voice_path_receipt import (
    emit_voice_path_llm_receipt,
    emit_voice_path_stt_receipt,
    emit_voice_path_tts_receipt,
)


def _make_on_event() -> AsyncMock:
    return AsyncMock()


def _last_payload(on_event: AsyncMock) -> dict:
    return on_event.await_args.args[0]


# ─── STT receipt ────────────────────────────────────────────


class TestSttReceipt:
    @pytest.mark.asyncio
    async def test_emits_stage_stt_with_backend_and_ms(self):
        on_event = _make_on_event()
        await emit_voice_path_stt_receipt(
            on_event, stt_backend="moonshine", stt_ms=423.7,
        )
        p = _last_payload(on_event)
        assert p["type"] == "receipt"
        assert p["stage"] == "stt"
        assert p["model"] == "moonshine"
        assert p["stt_ms"] == 424  # rounded
        assert p["cost_mils"] == 0  # no per-second cost yet

    @pytest.mark.asyncio
    async def test_empty_backend_falls_back_to_string_stt(self):
        on_event = _make_on_event()
        await emit_voice_path_stt_receipt(
            on_event, stt_backend="", stt_ms=10,
        )
        assert _last_payload(on_event)["model"] == "stt"

    @pytest.mark.asyncio
    async def test_emit_failure_swallowed(self):
        on_event = AsyncMock(side_effect=ConnectionResetError("dead"))
        # Must NOT raise.
        await emit_voice_path_stt_receipt(
            on_event, stt_backend="moonshine", stt_ms=10,
        )


# ─── LLM receipt ────────────────────────────────────────────


def _make_supports_usage_llm(usage: dict) -> MagicMock:
    """Build an LLM that satisfies the SupportsUsage protocol
    by exposing get_last_usage as a real method."""
    from dragon_voice.llm.base import SupportsUsage

    class _Backend:
        name = "test_llm"

        def get_last_usage(self) -> dict:
            return usage

    inst = _Backend()
    assert isinstance(inst, SupportsUsage)
    return inst


class TestLlmReceipt:
    @pytest.mark.asyncio
    async def test_no_supports_usage_skips(self):
        """Backends like `dual` or `tinkerclaw` that don't
        implement SupportsUsage MUST be silently skipped (Wave
        21b #204 closure pin)."""
        on_event = _make_on_event()
        plain = MagicMock(spec=[])  # no get_last_usage at all
        await emit_voice_path_llm_receipt(
            on_event, llm=plain, llm_ms=100,
        )
        on_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_zero_total_tokens_skips(self):
        llm = _make_supports_usage_llm({
            "model": "anthropic/claude-haiku-4.5",
            "total_tokens": 0,
        })
        on_event = _make_on_event()
        await emit_voice_path_llm_receipt(
            on_event, llm=llm, llm_ms=100,
        )
        on_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_happy_path_full_receipt(self):
        llm = _make_supports_usage_llm({
            "model": "anthropic/claude-haiku-4.5",
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        })
        on_event = _make_on_event()
        await emit_voice_path_llm_receipt(
            on_event, llm=llm, llm_ms=2500,
        )
        p = _last_payload(on_event)
        assert p["type"] == "receipt"
        assert p["stage"] == "llm"
        assert p["model"] == "anthropic/claude-haiku-4.5"
        assert p["prompt_tokens"] == 100
        assert p["completion_tokens"] == 50
        assert p["total_tokens"] == 150
        assert p["cost_mils"] > 0  # haiku has real pricing
        assert p["llm_ms"] == 2500
        assert p["retried"] is False
        assert p["retry_reason"] == ""

    @pytest.mark.asyncio
    async def test_retried_field_surfaced(self):
        """v4·D Gauntlet G2: when usage carries retried=True
        the receipt MUST surface it so Tab5 can stamp a
        'RETRIED' chip."""
        llm = _make_supports_usage_llm({
            "model": "anthropic/claude-sonnet-4.6",
            "prompt_tokens": 200,
            "completion_tokens": 100,
            "total_tokens": 300,
            "retried": True,
            "retry_reason": "context_trim",
        })
        on_event = _make_on_event()
        await emit_voice_path_llm_receipt(
            on_event, llm=llm, llm_ms=4000,
        )
        p = _last_payload(on_event)
        assert p["retried"] is True
        assert p["retry_reason"] == "context_trim"

    @pytest.mark.asyncio
    async def test_v4d_audit_p0_fallback_on_pricing_failure(self):
        """v4·D audit P0 closure: when the usage-based emit
        fails (corrupt usage, pricing-table miss), MUST emit a
        MINIMAL receipt with cost_mils=0 + zero token counts so
        the chat bubble still gets a stamp + the day-budget
        accumulator stays consistent."""
        llm = _make_supports_usage_llm({
            "model": "unknown/model-not-in-pricing",
            "total_tokens": 10,
        })
        on_event = _make_on_event()

        # Simulate price_for_model raising — exercises the
        # except-branch fallback emit.
        with patch(
            "dragon_voice.llm.openrouter_llm.price_for_model",
            side_effect=KeyError("unknown model"),
        ):
            await emit_voice_path_llm_receipt(
                on_event, llm=llm, llm_ms=1000,
            )

        # Fallback receipt fired with zero tokens + cost
        p = _last_payload(on_event)
        assert p["type"] == "receipt"
        assert p["stage"] == "llm"
        assert p["model"] == "test_llm"  # llm.name fallback
        assert p["prompt_tokens"] == 0
        assert p["completion_tokens"] == 0
        assert p["total_tokens"] == 0
        assert p["cost_mils"] == 0
        assert p["llm_ms"] == 1000
        assert p["retried"] is False
        # Encodes the failure cause for ops triage
        assert p["retry_reason"].startswith("receipt-fallback: KeyError")

    @pytest.mark.asyncio
    async def test_double_failure_swallowed(self):
        """If even the fallback emit raises, log at DEBUG and
        return — never propagate."""
        llm = _make_supports_usage_llm({
            "model": "x", "total_tokens": 1,
        })

        # First emit (usage path) AND second emit (fallback)
        # both raise.
        on_event = AsyncMock(side_effect=RuntimeError("dead"))

        with patch(
            "dragon_voice.llm.openrouter_llm.price_for_model",
            side_effect=KeyError("miss"),
        ):
            # Must NOT raise.
            await emit_voice_path_llm_receipt(
                on_event, llm=llm, llm_ms=10,
            )


# ─── TTS receipt ────────────────────────────────────────────


class TestTtsReceipt:
    @pytest.mark.asyncio
    async def test_emits_stage_tts_with_backend_and_ms(self):
        on_event = _make_on_event()
        await emit_voice_path_tts_receipt(
            on_event, tts_backend="piper", tts_total_ms=850.3,
        )
        p = _last_payload(on_event)
        assert p["type"] == "receipt"
        assert p["stage"] == "tts"
        assert p["model"] == "piper"
        assert p["tts_ms"] == 850
        assert p["cost_mils"] == 0

    @pytest.mark.asyncio
    async def test_zero_tts_ms_is_noop(self):
        """Pin: no audio synthesised this turn (silence reply
        or cancelled mid-stream) → no receipt fires."""
        on_event = _make_on_event()
        await emit_voice_path_tts_receipt(
            on_event, tts_backend="piper", tts_total_ms=0,
        )
        on_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_negative_tts_ms_is_noop(self):
        """Defensive: pathological negative timing → silent skip."""
        on_event = _make_on_event()
        await emit_voice_path_tts_receipt(
            on_event, tts_backend="piper", tts_total_ms=-5,
        )
        on_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_backend_falls_back_to_string_tts(self):
        on_event = _make_on_event()
        await emit_voice_path_tts_receipt(
            on_event, tts_backend="", tts_total_ms=100,
        )
        assert _last_payload(on_event)["model"] == "tts"

    @pytest.mark.asyncio
    async def test_emit_failure_swallowed(self):
        on_event = AsyncMock(side_effect=ConnectionResetError("dead"))
        # Must NOT raise.
        await emit_voice_path_tts_receipt(
            on_event, tts_backend="piper", tts_total_ms=100,
        )
