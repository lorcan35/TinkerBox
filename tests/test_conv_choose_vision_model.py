"""Tests for `ConversationEngine.choose_vision_model` (audit ENC-1).

Pre-extract `server.py` reached directly into `self._conversation._llm`
and called `.choose(...)` on the router instance from three sites.
That violated encapsulation (private attribute access) AND DIP (high-
level WS handler depending on a concrete `CapabilityAwareRouter` type
rather than a stable public surface).

These tests pin three contracts:
  1. Returns `None` when the active backend isn't a router (single-
     backend configurations) — caller falls through to substring gate.
  2. Returns the router's `.choose(...)` result verbatim when the
     router has a vision-capable candidate for the given tier.
  3. Returns `None` when the router has no vision-capable candidate
     for the requested voice_mode tier — caller falls through.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from dragon_voice.config import LLMConfig
from dragon_voice.conversation import ConversationEngine


def _make_engine() -> ConversationEngine:
    return ConversationEngine(
        db=MagicMock(),
        message_store=MagicMock(),
        llm_config=LLMConfig(backend="ollama", ollama_model="ministral-3:3b"),
    )


def test_returns_none_when_backend_is_not_router():
    """Single-backend configurations (e.g. plain Ollama) return None
    so the caller can fall back to the substring-based capability
    gate that handles single-backend setups."""
    eng = _make_engine()
    fake_backend = MagicMock()
    fake_backend.name = "ollama-fake"
    eng._llm = fake_backend  # not a CapabilityAwareRouter

    assert eng.choose_vision_model(voice_mode=2) is None


def test_returns_none_when_llm_is_none():
    """Pre-initialize() state should not crash."""
    eng = _make_engine()
    assert eng._llm is None
    assert eng.choose_vision_model(voice_mode=2) is None


def test_returns_router_choose_result_when_router_active():
    """Router gets the {TEXT, VISION} cap set + the voice_mode tier
    forwarded straight through.  The returned ModelSpec is whatever
    the router picked."""
    from dragon_voice.llm.base import Modality
    from dragon_voice.llm.router import CapabilityAwareRouter, ModelSpec

    eng = _make_engine()
    router = MagicMock(spec=CapabilityAwareRouter)
    expected_spec = ModelSpec(
        id="qwen36_flash",
        backend="openrouter",
        model_id="qwen/qwen3.6-flash",
        capabilities=frozenset({Modality.TEXT, Modality.VISION}),
        tier="cloud",
        priority=5,
    )
    router.choose.return_value = expected_spec
    eng._llm = router

    out = eng.choose_vision_model(voice_mode=2)

    assert out is expected_spec
    # Pin the call shape: TEXT+VISION cap set + the integer voice_mode
    router.choose.assert_called_once_with(
        {Modality.TEXT, Modality.VISION}, 2,
    )


def test_returns_none_when_router_has_no_vision_candidate():
    """Router with no vision-capable model for this tier returns
    None.  Caller falls through to substring-gate (or just doesn't
    advertise a vision model)."""
    from dragon_voice.llm.router import CapabilityAwareRouter

    eng = _make_engine()
    router = MagicMock(spec=CapabilityAwareRouter)
    router.choose.return_value = None  # no candidate
    eng._llm = router

    assert eng.choose_vision_model(voice_mode=0) is None


def test_voice_mode_forwarded_to_router_choose():
    """The integer voice_mode is forwarded verbatim — router uses it
    to gate against its TIER_FOR_MODE map."""
    from dragon_voice.llm.base import Modality
    from dragon_voice.llm.router import CapabilityAwareRouter

    eng = _make_engine()
    router = MagicMock(spec=CapabilityAwareRouter)
    router.choose.return_value = None
    eng._llm = router

    for mode in (0, 1, 2, 3, 4):
        eng.choose_vision_model(voice_mode=mode)

    # Last call's voice_mode should equal 4
    args, _ = router.choose.call_args
    assert args == ({Modality.TEXT, Modality.VISION}, 4)
    assert router.choose.call_count == 5
