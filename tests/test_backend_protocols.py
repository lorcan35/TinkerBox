"""Tests for the optional-feature Protocol mixins (#204, Wave 21b).

The four `Supports*` Protocols in `dragon_voice/llm/base.py` replace the
hasattr() pattern that used to gate optional backend methods. These
tests pin the structural matrix — which backends implement which
Protocols — so a future regression where a backend silently drops a
method (or where dual.py stops forwarding) fails loudly.
"""

import importlib
import sys
import types

import pytest

from dragon_voice.config import LLMConfig
from dragon_voice.llm.base import (
    SupportsClearHistory,
    SupportsHistoryTrim,
    SupportsSessionKey,
    SupportsUsage,
)


# ─────────────────────────────────────────────────────────────────────
# Per-backend Protocol matrix
#
# Pre-Wave-21b every callsite did `hasattr(...)` against a method name
# string. Now they use `isinstance(backend, SupportsX)`. These tests
# ensure the *same* backends that used to satisfy hasattr now satisfy
# isinstance — and that the Protocols are runtime-checkable.
# ─────────────────────────────────────────────────────────────────────


def _make_ollama() -> object:
    from dragon_voice.llm.ollama_llm import OllamaBackend

    return OllamaBackend(LLMConfig(backend="ollama", ollama_model="ministral-3:3b"))


def _make_openrouter() -> object:
    from dragon_voice.llm.openrouter_llm import OpenRouterBackend

    return OpenRouterBackend(
        LLMConfig(
            backend="openrouter",
            openrouter_api_key="sk-test",
            openrouter_model="anthropic/claude-3.5-haiku",
        )
    )


def _make_lmstudio() -> object:
    from dragon_voice.llm.lmstudio_llm import LMStudioBackend

    return LMStudioBackend(
        LLMConfig(
            backend="lmstudio",
            lmstudio_url="http://localhost:1234/v1",
            lmstudio_model="some-gguf",
        )
    )


def _make_npu_genie() -> object:
    """The genie module imports a runtime SDK at module-load. Stub if needed."""
    try:
        from dragon_voice.llm.npu_genie import NpuGenieBackend
    except ImportError as exc:  # pragma: no cover — only on missing QAIRT SDK
        pytest.skip(f"npu_genie unavailable in test env: {exc}")
    return NpuGenieBackend(LLMConfig(backend="npu_genie"))


def _make_tinkerclaw() -> object:
    from dragon_voice.llm.tinkerclaw_llm import TinkerClawBackend

    return TinkerClawBackend(
        LLMConfig(
            backend="tinkerclaw",
            tinkerclaw_url="http://localhost:18789",
            tinkerclaw_token="dummy-test-token",
            tinkerclaw_model="anthropic/claude-3.5-haiku",
        )
    )


def _make_dual() -> object:
    from dragon_voice.llm.dual import DualModelBackend

    return DualModelBackend(
        LLMConfig(
            backend="dual",
            dual_picker_backend="ollama",
            dual_picker_model="xlam-2:1b",
            dual_responder_backend="ollama",
            dual_responder_model="ministral-3:3b",
        )
    )


# Matrix of (backend_factory, expected protocols).
#
# Sourced from `grep -E "def (set_session_key|get_last_usage|trim_history|clear_history)"`
# across the llm package as of 2026-05-02. If a backend gains/loses a
# method this test will catch the drift and force an explicit decision.
_BACKEND_MATRIX: list[tuple[str, callable, set[type]]] = [
    (
        "ollama",
        _make_ollama,
        {SupportsUsage, SupportsHistoryTrim, SupportsClearHistory},
    ),
    (
        "openrouter",
        _make_openrouter,
        {SupportsUsage, SupportsHistoryTrim, SupportsClearHistory},
    ),
    (
        "lmstudio",
        _make_lmstudio,
        {SupportsHistoryTrim, SupportsClearHistory},
    ),
    (
        "tinkerclaw",
        _make_tinkerclaw,
        {SupportsSessionKey},
    ),
    (
        # Dual implements all three forwarders (set_session_key isn't
        # forwarded — it's TinkerClaw-specific and the responder is
        # almost never TC). Forwarders are no-ops if the responder
        # doesn't support; isinstance still returns True because the
        # method exists with the correct signature.
        "dual",
        _make_dual,
        {SupportsUsage, SupportsHistoryTrim, SupportsClearHistory},
    ),
]


@pytest.mark.parametrize("name,factory,expected", _BACKEND_MATRIX,
                          ids=[m[0] for m in _BACKEND_MATRIX])
def test_backend_protocol_matrix(name, factory, expected):
    backend = factory()
    all_protocols = {
        SupportsSessionKey,
        SupportsUsage,
        SupportsHistoryTrim,
        SupportsClearHistory,
    }
    actual = {p for p in all_protocols if isinstance(backend, p)}
    assert actual == expected, (
        f"{name} protocol matrix drift — "
        f"missing: {expected - actual} unexpected: {actual - expected}"
    )


# ─────────────────────────────────────────────────────────────────────
# Protocol semantics — runtime-checkable + structural
# ─────────────────────────────────────────────────────────────────────


def test_protocols_are_runtime_checkable():
    """All four Protocols must be @runtime_checkable for isinstance() to work."""
    for proto in (SupportsSessionKey, SupportsUsage, SupportsHistoryTrim,
                  SupportsClearHistory):
        # Plain objects without the methods should NOT match
        assert not isinstance(object(), proto)


def test_structural_match_via_duck_typing():
    """isinstance() should match any object with the right method, no inheritance needed."""

    class BareDuck:
        def get_last_usage(self) -> dict:
            return {"model": "fake", "total_tokens": 7}

    duck = BareDuck()
    assert isinstance(duck, SupportsUsage)
    assert not isinstance(duck, SupportsClearHistory)


# ─────────────────────────────────────────────────────────────────────
# Dual forwarders — concrete behavior
# ─────────────────────────────────────────────────────────────────────


def test_dual_forwards_get_last_usage_to_responder():
    """Dual.get_last_usage must surface the responder's usage, not literal 'llm'.

    This is the concrete bug from audit finding F5 — pre-Wave-21b,
    pipeline's hasattr check missed Dual entirely (no method) and
    receipts emitted model='llm'. Forwarder closes that.
    """
    dual = _make_dual()
    # Both picker + responder are ollama with empty _last_usage at construction.
    # The forwarder should return the responder's empty dict, not raise.
    usage = dual.get_last_usage()
    assert isinstance(usage, dict)
    # Now poke the responder to simulate a turn finishing
    dual._responder._last_usage = {  # type: ignore[attr-defined]
        "model": "ministral-3:3b",
        "prompt_tokens": 12,
        "completion_tokens": 7,
        "total_tokens": 19,
    }
    forwarded = dual.get_last_usage()
    assert forwarded["model"] == "ministral-3:3b"
    assert forwarded["total_tokens"] == 19


def test_dual_trim_history_forwards_to_responder():
    dual = _make_dual()
    # Seed a fake history on the responder
    dual._responder._conversation = [  # type: ignore[attr-defined]
        {"role": "user", "content": "1"},
        {"role": "assistant", "content": "2"},
        {"role": "user", "content": "3"},
        {"role": "assistant", "content": "4"},
    ]
    dual.trim_history(max_turns=1)
    # Ollama's trim keeps last (max_turns * 2) messages
    assert len(dual._responder._conversation) == 2  # type: ignore[attr-defined]


def test_dual_clear_history_forwards_to_responder():
    dual = _make_dual()
    dual._responder._conversation = [  # type: ignore[attr-defined]
        {"role": "user", "content": "x"},
    ]
    dual.clear_history()
    assert dual._responder._conversation == []  # type: ignore[attr-defined]


def test_dual_does_not_implement_session_key():
    """SupportsSessionKey is TinkerClaw-specific; Dual deliberately omits."""
    dual = _make_dual()
    assert not isinstance(dual, SupportsSessionKey)
