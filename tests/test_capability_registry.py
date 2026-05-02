"""Tests for `dragon_voice/llm/capability_registry.py` (#200, Wave 21).

The registry centralizes capability detection that used to live as
inline heuristics in each backend's `capabilities` property. These
tests pin both:

  1. The detector functions themselves (pure, model_id → frozenset).
  2. The register / detect public API.

The existing `test_capability_declaration.py` exercises each backend's
`capabilities` property end-to-end through its constructor, which
covers the integration. The tests below cover the registry layer in
isolation so a future detector change can be unit-tested without
spinning up backend instances.
"""

import pytest

from dragon_voice.llm.base import Modality
from dragon_voice.llm.capability_registry import (
    detect,
    detect_lmstudio,
    detect_npu_genie,
    detect_ollama,
    detect_tinkerclaw,
    register,
    registered_backends,
)


# ─────────────────────────────────────────────────────────────────────
# Public register / detect API
# ─────────────────────────────────────────────────────────────────────


def test_built_ins_registered_at_import():
    backends = registered_backends()
    assert "ollama" in backends
    assert "openrouter" in backends
    assert "lmstudio" in backends
    assert "npu_genie" in backends
    assert "tinkerclaw" in backends


def test_unknown_backend_defaults_to_text_only():
    caps = detect("nonexistent_backend", "any-model-id")
    assert caps == frozenset({Modality.TEXT})


def test_register_and_detect_roundtrip():
    sentinel = frozenset({Modality.TEXT, Modality.AUDIO_OUT})

    def fake_detector(_model_id: str) -> frozenset[Modality]:
        return sentinel

    register("__test_fake_backend__", fake_detector)
    try:
        assert detect("__test_fake_backend__", "any") == sentinel
    finally:
        # Cleanup so the global registry doesn't carry the fake into
        # other tests in the same pytest session.
        from dragon_voice.llm.capability_registry import _DETECTORS

        _DETECTORS.pop("__test_fake_backend__", None)


def test_detect_passes_empty_string_when_model_id_is_none():
    """Defensive: callers pass `model_id=None` for unconfigured backends."""
    # npu_genie ignores the argument so this is the safest detector to
    # check the contract: detect() should normalize None → "" before
    # calling the detector.
    caps = detect("npu_genie", None)  # type: ignore[arg-type]
    assert caps == frozenset({Modality.TEXT})


# ─────────────────────────────────────────────────────────────────────
# Ollama detector
# ─────────────────────────────────────────────────────────────────────


def test_ollama_text_only_default():
    assert detect_ollama("some-random-model:7b") == frozenset({Modality.TEXT})
    assert detect_ollama("") == frozenset({Modality.TEXT})


@pytest.mark.parametrize(
    "model_id",
    [
        "llava:7b",
        "bakllava:13b",
        "minicpm-v:latest",
        "moondream",
        "hf.co/openbmb/MiniCPM-V-4-gguf:Q4_K_M",
        "qwen2-vl:7b",
        "pixtral:12b",
        "internvl:1b",
        "llama3.2-vision:11b",
    ],
)
def test_ollama_vision_models_get_vision_and_video(model_id):
    caps = detect_ollama(model_id)
    assert Modality.VISION in caps
    assert Modality.VIDEO in caps


def test_ollama_minicpm_o_adds_audio():
    caps = detect_ollama("minicpm-o:8b")
    assert Modality.AUDIO_IN in caps
    assert Modality.AUDIO_OUT in caps
    # And vision (it's in the vision-hint list too)
    assert Modality.VISION in caps


@pytest.mark.parametrize(
    "model_id,expected_tool",
    [
        ("ministral-3:3b", True),
        ("gemma3:4b", True),
        ("xlam-2:1b", True),
        ("llama3.1:8b", True),
        ("hermes3:3b", True),
        ("qwen2.5:3b", True),
        ("functiongemma:2b", True),
        # Not in the FC family list:
        ("phi4-mini", False),
        ("qwen3:1.7b", False),
        ("llama3.2:3b", True),  # in the list
    ],
)
def test_ollama_tool_calling_detection(model_id, expected_tool):
    caps = detect_ollama(model_id)
    assert (Modality.TOOL_CALLING in caps) is expected_tool


# ─────────────────────────────────────────────────────────────────────
# LM Studio detector
# ─────────────────────────────────────────────────────────────────────


def test_lmstudio_always_declares_tool_calling():
    """LM Studio's /chat/completions accepts OpenAI tools across the board."""
    assert Modality.TOOL_CALLING in detect_lmstudio("random-gguf-name")
    assert Modality.TOOL_CALLING in detect_lmstudio("llava:7b")
    assert Modality.TOOL_CALLING in detect_lmstudio("")


def test_lmstudio_vision_detection_matches_ollama():
    caps = detect_lmstudio("hf.co/openbmb/MiniCPM-V-4-gguf:Q4_K_M")
    assert Modality.VISION in caps
    assert Modality.VIDEO in caps
    assert Modality.TEXT in caps
    assert Modality.TOOL_CALLING in caps


# ─────────────────────────────────────────────────────────────────────
# NPU Genie detector
# ─────────────────────────────────────────────────────────────────────


def test_npu_genie_always_text_only():
    """QAIRT / Genie has no vision / audio path today."""
    for model_id in ("", "llama3.2-1b.bin", "any/path", "llava-genie.bin"):
        assert detect_npu_genie(model_id) == frozenset({Modality.TEXT})


# ─────────────────────────────────────────────────────────────────────
# TinkerClaw detector
# ─────────────────────────────────────────────────────────────────────


def test_tinkerclaw_baseline_text_plus_tools():
    caps = detect_tinkerclaw("some-unknown/model")
    assert caps == frozenset({Modality.TEXT, Modality.TOOL_CALLING})
    assert detect_tinkerclaw("") == frozenset({Modality.TEXT, Modality.TOOL_CALLING})


@pytest.mark.parametrize(
    "model_id",
    [
        "anthropic/claude-sonnet-4.6",
        "openai/gpt-4o",
        "google/gemini-3-flash-preview",
        "minimax/abab-7b",
    ],
)
def test_tinkerclaw_known_multimodal_upstreams_get_vision(model_id):
    caps = detect_tinkerclaw(model_id)
    assert Modality.VISION in caps
    assert Modality.TEXT in caps
    assert Modality.TOOL_CALLING in caps


def test_tinkerclaw_unknown_upstream_no_vision():
    assert Modality.VISION not in detect_tinkerclaw("deepseek/deepseek-v4-flash")


# ─────────────────────────────────────────────────────────────────────
# OpenRouter detector — delegates to the static registry
# ─────────────────────────────────────────────────────────────────────


def test_openrouter_delegates_to_static_registry():
    """detect("openrouter", ...) should match _openrouter_capabilities()."""
    from dragon_voice.llm.openrouter_llm import _openrouter_capabilities

    for model_id in (
        "anthropic/claude-sonnet-4.6",
        "openai/gpt-4o",
        "deepseek/deepseek-v4-flash",
        "totally-unknown-model",
        "",
    ):
        assert detect("openrouter", model_id) == _openrouter_capabilities(model_id)


def test_openrouter_unknown_model_defaults_to_text_plus_tools():
    caps = detect("openrouter", "vendor/never-seen-model")
    assert caps == frozenset({Modality.TEXT, Modality.TOOL_CALLING})
