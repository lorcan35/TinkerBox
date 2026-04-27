"""CapabilityAwareRouter unit tests (#183 PR 2).

Cover:
- Spec parsing from YAML-style dicts
- choose() picks lowest priority + tier filter + cap match
- Voice mode -> tier policy
- summarize() returns per-modality model_id picks
- Lazy instantiation + shutdown propagation
- Empty fleet rejected
- generate_stream_with_messages infers caps from message content
"""

from unittest.mock import AsyncMock, patch

import pytest

from dragon_voice.config import LLMConfig
from dragon_voice.llm.base import LLMBackend, Modality
from dragon_voice.llm.router import (
    CapabilityAwareRouter,
    ModelSpec,
    TIER_FOR_MODE,
    _spec_from_dict,
    infer_required_caps,
)


# ── Fleet fixture ─────────────────────────────────────────────────
def _fleet_dict() -> list[dict]:
    return [
        # Local tier
        {"id": "ministral", "backend": "ollama", "model_id": "ministral-3:3b",
         "caps": ["text", "tool_calling"], "tier": "local", "priority": 0},
        {"id": "minicpm_v4", "backend": "ollama",
         "model_id": "hf.co/openbmb/MiniCPM-V-4-gguf:Q4_K_M",
         "caps": ["text", "vision", "video"], "tier": "local", "priority": 10},
        {"id": "minicpm_o45", "backend": "ollama",
         "model_id": "hf.co/openbmb/MiniCPM-o-4_5-gguf:Q4_K_M",
         "caps": ["text", "vision", "video", "audio_in", "audio_out"],
         "tier": "local", "priority": 20},
        # Cloud tier
        {"id": "haiku", "backend": "openrouter",
         "model_id": "anthropic/claude-3.5-haiku",
         "caps": ["text", "vision", "tool_calling"],
         "tier": "cloud", "priority": 5},
        {"id": "gemini_video", "backend": "openrouter",
         "model_id": "google/gemini-2.5-flash",
         "caps": ["text", "vision", "video", "tool_calling"],
         "tier": "cloud", "priority": 8},
    ]


def _router(extra_overrides: dict | None = None) -> CapabilityAwareRouter:
    cfg = LLMConfig(backend="router", fleet=_fleet_dict())
    if extra_overrides:
        for k, v in extra_overrides.items():
            setattr(cfg, k, v)
    return CapabilityAwareRouter(cfg)


# ── Spec parsing ──────────────────────────────────────────────────
def test_spec_from_dict_minimal():
    spec = _spec_from_dict({"id": "x", "backend": "ollama", "model_id": "m"})
    assert spec.id == "x"
    assert spec.tier == "local"
    assert spec.priority == 0
    assert spec.capabilities == frozenset({Modality.TEXT})


def test_spec_from_dict_caps_alias():
    """`caps` and `capabilities` both work as keys."""
    a = _spec_from_dict({"id": "a", "backend": "ollama", "model_id": "m",
                         "caps": ["text", "vision"]})
    b = _spec_from_dict({"id": "b", "backend": "ollama", "model_id": "m",
                         "capabilities": ["text", "vision"]})
    assert a.capabilities == b.capabilities


def test_spec_from_dict_overrides_for_lmstudio_url():
    spec = _spec_from_dict({"id": "dgx", "backend": "lmstudio", "model_id": "x",
                            "lmstudio_url": "http://workstation:1234/v1"})
    assert spec.overrides["lmstudio_url"] == "http://workstation:1234/v1"


def test_spec_from_dict_rejects_missing_required():
    with pytest.raises(ValueError, match="missing required field"):
        _spec_from_dict({"backend": "ollama"})


def test_spec_from_dict_rejects_bad_tier():
    with pytest.raises(ValueError, match="tier must be"):
        _spec_from_dict({"id": "x", "backend": "ollama", "model_id": "m",
                         "tier": "rogue"})


# ── Router init ───────────────────────────────────────────────────
def test_router_rejects_empty_fleet():
    cfg = LLMConfig(backend="router", fleet=[])
    with pytest.raises(ValueError, match="non-empty"):
        CapabilityAwareRouter(cfg)


def test_router_init_logs_fleet_summary():
    r = _router()
    assert len(r._fleet) == 5
    assert r._voice_mode == 0


# ── choose() ──────────────────────────────────────────────────────
def test_choose_local_text_picks_ministral():
    r = _router()
    spec = r.choose({Modality.TEXT}, voice_mode=0)
    assert spec.id == "ministral"


def test_choose_local_vision_picks_minicpm_v4_not_o45():
    """Lower priority wins — V4 (priority 10) beats o4.5 (priority 20)."""
    r = _router()
    spec = r.choose({Modality.TEXT, Modality.VISION}, voice_mode=0)
    assert spec.id == "minicpm_v4"


def test_choose_local_audio_picks_minicpm_o45():
    """Only o4.5 declares audio in the local fleet."""
    r = _router()
    spec = r.choose({Modality.TEXT, Modality.AUDIO_IN}, voice_mode=0)
    assert spec.id == "minicpm_o45"


def test_choose_cloud_vision_picks_haiku_lowest_priority():
    r = _router()
    spec = r.choose({Modality.TEXT, Modality.VISION}, voice_mode=2)
    assert spec.id == "haiku"


def test_choose_cloud_video_picks_gemini():
    """Video is only on gemini in the cloud fleet."""
    r = _router()
    spec = r.choose({Modality.TEXT, Modality.VIDEO}, voice_mode=2)
    assert spec.id == "gemini_video"


def test_choose_local_audio_in_cloud_mode_returns_none():
    """Cloud fleet has no audio model; returns None."""
    r = _router()
    spec = r.choose({Modality.TEXT, Modality.AUDIO_IN}, voice_mode=2)
    assert spec is None


def test_choose_tinkerclaw_mode_returns_none():
    """voice_mode=3 short-circuits the router."""
    r = _router()
    spec = r.choose({Modality.TEXT}, voice_mode=3)
    assert spec is None


def test_choose_hybrid_uses_local_tier():
    """Hybrid (mode 1) keeps LLM local."""
    r = _router()
    spec = r.choose({Modality.TEXT}, voice_mode=1)
    assert spec.id == "ministral"
    assert spec.tier == "local"


# ── set_voice_mode ────────────────────────────────────────────────
def test_set_voice_mode_changes_routing():
    r = _router()
    spec0 = r.choose({Modality.TEXT, Modality.VISION}, voice_mode=0)
    r.set_voice_mode(2)
    spec2 = r.choose({Modality.TEXT, Modality.VISION}, voice_mode=2)
    assert spec0.id == "minicpm_v4"
    assert spec2.id == "haiku"


# ── capabilities (tier-restricted union) ──────────────────────────
def test_capabilities_local_mode_union():
    r = _router()
    assert Modality.VISION in r.capabilities
    assert Modality.AUDIO_IN in r.capabilities  # minicpm_o45 has it


def test_capabilities_cloud_mode_no_audio():
    r = _router()
    r.set_voice_mode(2)
    assert Modality.VISION in r.capabilities
    assert Modality.VIDEO in r.capabilities      # gemini_video has it
    assert Modality.AUDIO_IN not in r.capabilities  # cloud fleet has no audio


def test_capabilities_tinkerclaw_mode_empty():
    r = _router()
    r.set_voice_mode(3)
    assert r.capabilities == frozenset()


# ── summarize() ───────────────────────────────────────────────────
def test_summarize_local_mode():
    r = _router()
    summary = r.summarize(voice_mode=0)
    assert summary["text"] == "ministral-3:3b"
    assert summary["vision"] == "hf.co/openbmb/MiniCPM-V-4-gguf:Q4_K_M"
    assert summary["audio_in"] == "hf.co/openbmb/MiniCPM-o-4_5-gguf:Q4_K_M"


def test_summarize_cloud_mode():
    r = _router()
    summary = r.summarize(voice_mode=2)
    # Lowest-priority text-capable cloud model is haiku (priority 5)
    assert summary["text"] == "anthropic/claude-3.5-haiku"
    assert summary["vision"] == "anthropic/claude-3.5-haiku"
    assert summary["video"] == "google/gemini-2.5-flash"
    assert summary["audio_in"] is None  # no cloud audio in fleet


# ── infer_required_caps() ─────────────────────────────────────────
def test_infer_text_only():
    msgs = [{"role": "user", "content": "hello"}]
    assert infer_required_caps(msgs) == frozenset({Modality.TEXT})


def test_infer_vision_from_image_url():
    msgs = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:..."}},
        {"type": "text", "text": "describe"},
    ]}]
    caps = infer_required_caps(msgs)
    assert Modality.VISION in caps
    assert Modality.TEXT in caps


def test_infer_video_from_video_url():
    msgs = [{"role": "user", "content": [
        {"type": "video_url", "video_url": {"url": "x"}},
        {"type": "text", "text": "describe"},
    ]}]
    assert Modality.VIDEO in infer_required_caps(msgs)


def test_infer_audio_from_input_audio():
    msgs = [{"role": "user", "content": [
        {"type": "input_audio", "input_audio": {"data": "x"}},
    ]}]
    assert Modality.AUDIO_IN in infer_required_caps(msgs)


# ── Lazy instantiation + delegate (mocked) ────────────────────────
class _FakeBackend(LLMBackend):
    def __init__(self, *a, tokens=("a", "b"), **kw):
        self.tokens = tokens
        self.initialized = False
        self.shutdown_called = False

    async def initialize(self):
        self.initialized = True

    async def generate_stream(self, prompt, system_prompt=""):
        for t in self.tokens:
            yield t

    async def generate_stream_with_messages(self, messages):
        for t in self.tokens:
            yield t

    async def shutdown(self):
        self.shutdown_called = True

    @property
    def name(self):
        return "fake"


@pytest.mark.asyncio
async def test_lazy_instantiation_and_delegate():
    """First request instantiates the picked backend; second reuses it."""
    r = _router()
    fakes_created: list[_FakeBackend] = []
    def _create(_cfg):
        fb = _FakeBackend()
        fakes_created.append(fb)
        return fb
    with patch("dragon_voice.llm.create_llm", side_effect=_create):
        msgs = [{"role": "user", "content": "hi"}]
        out1 = [t async for t in r.generate_stream_with_messages(msgs)]
        out2 = [t async for t in r.generate_stream_with_messages(msgs)]
    assert out1 == ["a", "b"]
    assert out2 == ["a", "b"]
    assert len(fakes_created) == 1  # reuse on second call
    assert fakes_created[0].initialized is True


@pytest.mark.asyncio
async def test_shutdown_propagates_to_all_instances():
    r = _router()
    fakes: list[_FakeBackend] = []
    def _create(_cfg):
        fb = _FakeBackend()
        fakes.append(fb)
        return fb
    with patch("dragon_voice.llm.create_llm", side_effect=_create):
        # Force two different specs to instantiate
        msgs_text = [{"role": "user", "content": "hi"}]  # → ministral
        msgs_vision = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:..."}},
            {"type": "text", "text": "?"},
        ]}]  # → minicpm_v4
        async for _ in r.generate_stream_with_messages(msgs_text): pass
        async for _ in r.generate_stream_with_messages(msgs_vision): pass
    assert len(fakes) == 2
    await r.shutdown()
    assert all(f.shutdown_called for f in fakes)


@pytest.mark.asyncio
async def test_no_match_yields_nothing():
    """Audio request in cloud-only fleet → empty stream (caller emits error)."""
    r = _router()
    r.set_voice_mode(2)  # cloud — no audio model
    msgs = [{"role": "user", "content": [
        {"type": "input_audio", "input_audio": {"data": "x"}},
    ]}]
    out = [t async for t in r.generate_stream_with_messages(msgs)]
    assert out == []


# ── Tier policy ───────────────────────────────────────────────────
def test_tier_policy_table_complete():
    """Every Tab5 voice_mode (0..3) must have a tier policy."""
    for mode in (0, 1, 2, 3):
        assert mode in TIER_FOR_MODE
