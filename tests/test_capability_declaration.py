"""Verify each LLMBackend declares its modality capabilities correctly.

PR 1 of the multi-model router (#183) — these tests are the contract
the router will rely on. If you add a new backend or change a
capability heuristic, add a test row here.
"""

from dragon_voice.config import LLMConfig
from dragon_voice.llm.base import LLMBackend, Modality
from dragon_voice.llm.ollama_llm import OllamaBackend
from dragon_voice.llm.openrouter_llm import OpenRouterBackend, _openrouter_capabilities
from dragon_voice.llm.lmstudio_llm import LMStudioBackend
from dragon_voice.llm.npu_genie import NPUGenieBackend
from dragon_voice.llm.tinkerclaw_llm import TinkerClawBackend


# ── Base default ──────────────────────────────────────────────────
def test_base_default_is_text_only():
    """Anything inheriting from LLMBackend without override is text-only."""
    class _StubBackend(LLMBackend):
        async def initialize(self): pass
        async def generate_stream(self, prompt, system_prompt=""): yield ""
        async def shutdown(self): pass
        @property
        def name(self): return "stub"
    assert _StubBackend().capabilities == frozenset({Modality.TEXT})


# ── Ollama ────────────────────────────────────────────────────────
def _ollama_caps(model: str) -> frozenset[Modality]:
    cfg = LLMConfig(backend="ollama", ollama_model=model)
    return OllamaBackend(cfg).capabilities


def test_ollama_text_model_only_text_and_tools():
    caps = _ollama_caps("ministral-3:3b")
    assert Modality.TEXT in caps
    assert Modality.TOOL_CALLING in caps
    assert Modality.VISION not in caps
    assert Modality.AUDIO_IN not in caps


def test_ollama_minicpm_v_has_vision_and_video():
    caps = _ollama_caps("hf.co/openbmb/MiniCPM-V-4-gguf:Q4_K_M")
    assert {Modality.TEXT, Modality.VISION, Modality.VIDEO} <= caps
    assert Modality.AUDIO_IN not in caps  # V is vision-only, not omni


def test_ollama_minicpm_o_has_vision_video_audio():
    caps = _ollama_caps("hf.co/openbmb/MiniCPM-o-4_5-gguf:Q4_K_M")
    assert {Modality.TEXT, Modality.VISION, Modality.VIDEO,
            Modality.AUDIO_IN, Modality.AUDIO_OUT} <= caps


def test_ollama_llava_has_vision():
    caps = _ollama_caps("llava:7b")
    assert Modality.VISION in caps
    assert Modality.AUDIO_IN not in caps


def test_ollama_unknown_model_defaults_text():
    caps = _ollama_caps("some-experimental-model:latest")
    assert caps == frozenset({Modality.TEXT})


# ── OpenRouter ────────────────────────────────────────────────────
def _or_caps(model: str) -> frozenset[Modality]:
    cfg = LLMConfig(backend="openrouter", openrouter_model=model,
                    openrouter_api_key="sk-test")
    return OpenRouterBackend(cfg).capabilities


def test_openrouter_haiku_3_has_vision_and_tools():
    """Haiku 3.0 (older route) actually serves images cleanly.
    See test_openrouter_haiku_35_is_text_only_due_to_bedrock_route below
    for why 3.5 is treated text-only."""
    caps = _or_caps("anthropic/claude-3-haiku")
    assert {Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING} <= caps


def test_openrouter_gpt4o_has_audio():
    caps = _or_caps("openai/gpt-4o")
    assert {Modality.TEXT, Modality.VISION, Modality.AUDIO_IN,
            Modality.AUDIO_OUT, Modality.TOOL_CALLING} <= caps


def test_openrouter_gemini_flash_has_video():
    caps = _or_caps("google/gemini-2.5-flash")
    assert Modality.VIDEO in caps
    assert Modality.VISION in caps


def test_openrouter_unknown_falls_back_to_text_and_tools():
    caps = _openrouter_capabilities("some-experimental/model")
    assert caps == frozenset({Modality.TEXT, Modality.TOOL_CALLING})


def test_openrouter_empty_model_id_falls_back():
    caps = _openrouter_capabilities("")
    assert caps == frozenset({Modality.TEXT, Modality.TOOL_CALLING})


# ── 2026-04-27 fleet (verified live) ──────────────────────────────
def test_openrouter_haiku_35_is_text_only_due_to_bedrock_route():
    """Haiku 3.5 via OR's Bedrock route rejects images — declare text-only."""
    caps = _or_caps("anthropic/claude-3.5-haiku")
    assert Modality.VISION not in caps
    assert Modality.TEXT in caps
    assert Modality.TOOL_CALLING in caps


def test_openrouter_sonnet_46_has_vision_and_tools():
    caps = _or_caps("anthropic/claude-sonnet-4.6")
    assert {Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING} <= caps


def test_openrouter_haiku_45_has_vision():
    caps = _or_caps("anthropic/claude-haiku-4.5")
    assert Modality.VISION in caps


def test_openrouter_opus_47_has_vision():
    caps = _or_caps("anthropic/claude-opus-4.7")
    assert Modality.VISION in caps


def test_openrouter_gpt_55_has_vision():
    caps = _or_caps("openai/gpt-5.5")
    assert {Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING} <= caps


def test_openrouter_gemini_3_flash_preview_has_video_and_audio():
    caps = _or_caps("google/gemini-3-flash-preview")
    assert {Modality.VISION, Modality.VIDEO, Modality.AUDIO_IN} <= caps


def test_openrouter_gemini_31_pro_preview_has_video():
    caps = _or_caps("google/gemini-3.1-pro-preview")
    assert Modality.VIDEO in caps


def test_openrouter_deepseek_v4_pro_text_only():
    """DeepSeek V4 line is text-only across the board (no vision support)."""
    caps = _or_caps("deepseek/deepseek-v4-pro")
    assert Modality.VISION not in caps
    assert Modality.TEXT in caps


def test_openrouter_deepseek_v4_flash_text_only():
    caps = _or_caps("deepseek/deepseek-v4-flash")
    assert Modality.VISION not in caps


def test_openrouter_qwen_36_flash_has_vision():
    """Qwen 3.6 Flash (released 2026-04-27) — multimodal sleeper pick."""
    caps = _or_caps("qwen/qwen3.6-flash")
    assert {Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING} <= caps


def test_openrouter_kimi_k26_has_vision():
    caps = _or_caps("moonshotai/kimi-k2.6")
    assert Modality.VISION in caps


def test_openrouter_grok_420_has_vision():
    caps = _or_caps("x-ai/grok-4.20")
    assert Modality.VISION in caps


# ── Pricing registry (verified live 2026-04-27) ──────────────────
def test_pricing_registry_has_all_caps_models():
    """Every model in _OPENROUTER_CAPS should also be in pricing.

    Catches the silent-undercount bug where adding a cap entry without
    a matching pricing entry falls through to `_default` rates and
    misreports daily spend.
    """
    from dragon_voice.llm.openrouter_llm import (
        _OPENROUTER_CAPS, _PRICING_MILS_PER_M,
    )
    missing = [m for m in _OPENROUTER_CAPS if m not in _PRICING_MILS_PER_M]
    assert not missing, f"Models in caps but missing pricing: {missing}"


# ── LM Studio ─────────────────────────────────────────────────────
def _lmstudio_caps(model: str) -> frozenset[Modality]:
    cfg = LLMConfig(backend="lmstudio", lmstudio_model=model,
                    lmstudio_url="http://localhost:1234/v1")
    return LMStudioBackend(cfg).capabilities


def test_lmstudio_default_text_and_tools():
    caps = _lmstudio_caps("default")
    assert caps == frozenset({Modality.TEXT, Modality.TOOL_CALLING})


def test_lmstudio_minicpm_v_has_vision():
    caps = _lmstudio_caps("openbmb/minicpm-v-4")
    assert {Modality.TEXT, Modality.VISION, Modality.VIDEO,
            Modality.TOOL_CALLING} <= caps


def test_lmstudio_minicpm_o_has_audio():
    caps = _lmstudio_caps("openbmb/minicpm-o-4_5")
    assert {Modality.AUDIO_IN, Modality.AUDIO_OUT} <= caps


# ── NPU Genie ─────────────────────────────────────────────────────
def test_npu_genie_text_only():
    cfg = LLMConfig(backend="npu_genie",
                    genie_model_dir="/tmp/nonexistent",
                    genie_config="cfg.json")
    # NPU init reads the dir; we don't call initialize() here.
    backend = NPUGenieBackend(cfg)
    assert backend.capabilities == frozenset({Modality.TEXT})


# ── TinkerClaw ────────────────────────────────────────────────────
def _tc_caps(model: str) -> frozenset[Modality]:
    cfg = LLMConfig(backend="tinkerclaw", tinkerclaw_model=model,
                    tinkerclaw_url="http://localhost:18789",
                    tinkerclaw_token="ci-tc-token")
    return TinkerClawBackend(cfg).capabilities


def test_tinkerclaw_minimax_has_vision_and_tools():
    caps = _tc_caps("minimax/MiniMax-M2.5")
    assert {Modality.TEXT, Modality.VISION, Modality.TOOL_CALLING} <= caps


def test_tinkerclaw_anthropic_routed_has_vision():
    caps = _tc_caps("anthropic/claude-3.5-haiku")
    assert Modality.VISION in caps


def test_tinkerclaw_unknown_text_only_with_tools():
    caps = _tc_caps("local-only-model")
    assert caps == frozenset({Modality.TEXT, Modality.TOOL_CALLING})
