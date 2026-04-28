"""Reasoning-model auto-detection in OllamaBackend (#84).

Ollama 0.21+ strips the ``<think>`` block from reasoning-model output
into a separate ``thinking`` field, which leaves the user-visible
``content`` empty when the default token budget is consumed by the
reasoning step.  ``OllamaBackend`` detects reasoning families by name
and adds ``think: false`` to its payloads so the model goes straight
to the answer.

These tests pin the detection heuristic without hitting a live Ollama —
the LLM tests with network are local-only by design.  Pure-Python
construction + flag inspection.
"""
from __future__ import annotations

import pytest

from dragon_voice.config import LLMConfig
from dragon_voice.llm.ollama_llm import (
    OllamaBackend,
    _is_reasoning_model,
)


@pytest.mark.parametrize("model_id", [
    "qwen3:0.6b",
    "qwen3:1.7b",
    "qwen3:4b",
    "qwen3.5:4b",
    "deepseek-r1:7b",
    "qwq:32b",
    "phi4-reasoning:latest",
    "QWEN3:1.7B",          # case-insensitive
])
def test_reasoning_models_detected(model_id: str) -> None:
    """Known reasoning families are flagged.  Pin so a future regex
    tweak doesn't drop one and silently break local-mode replies."""
    assert _is_reasoning_model(model_id) is True


@pytest.mark.parametrize("model_id", [
    "ministral-3:3b",
    "gemma3:4b",
    "llama3.2:3b",
    "hermes3:3b",
    "phi4-mini:latest",
    "hf.co/openbmb/MiniCPM-V-4-gguf:Q4_K_M",
    "hf.co/Salesforce/xLAM-2-1b-fc-r-gguf:Q4_K_M",
    "qwen2.5:3b",          # qwen2.5 is NOT a reasoning model
])
def test_non_reasoning_models_pass_through(model_id: str) -> None:
    """Non-reasoning models don't trigger think:false.  Pin so we
    don't accidentally widen the heuristic to a model that needs its
    full output budget."""
    assert _is_reasoning_model(model_id) is False


def test_backend_sets_think_false_flag_for_reasoning() -> None:
    """Constructing the backend with a reasoning model id should set
    the per-instance flag so both payload sites pick it up."""
    cfg = LLMConfig(backend="ollama", ollama_model="qwen3:1.7b")
    backend = OllamaBackend(cfg)
    assert backend._send_think_false is True


def test_backend_default_for_non_reasoning() -> None:
    """The default ministral path must NOT set think:false — it's not
    a reasoning model and Ollama ignores the field but the explicit
    test pins behavior."""
    cfg = LLMConfig(backend="ollama", ollama_model="ministral-3:3b")
    backend = OllamaBackend(cfg)
    assert backend._send_think_false is False
