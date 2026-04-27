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


def test_openrouter_haiku_has_vision_and_tools():
    caps = _or_caps("anthropic/claude-3.5-haiku")
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
