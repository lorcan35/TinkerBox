"""Tests for ``dragon_voice.config_swap.select_backends_for_mode``.

Pin the full mode-tier → backend-triple mapping that previously lived
inline in ``server.py:_handle_config_update`` (lines 2152-2212).

Per VoiceMode, four assertions:
  1. The right STT/TTS/LLM triple is returned.
  2. The mode-specific system_prompt is applied to ``conn_config.llm``.
  3. The mode-specific max_tokens is applied.
  4. The right model-id field on conn_config is updated when an
     ``llm_model`` arg is passed (and skipped when not relevant).

Plus dedicated tests for the TinkerClaw-with-"cloud"-suffix override,
the local-mode "vendor/model" rejection, and the TinkerClaw skip-
prompt path.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass, field

from dragon_voice.config import (
    SYSTEM_PROMPT_CLOUD,
    SYSTEM_PROMPT_HYBRID,
    SYSTEM_PROMPT_LOCAL,
    MAX_TOKENS_CLOUD,
    MAX_TOKENS_HYBRID,
    MAX_TOKENS_LOCAL,
)
from dragon_voice.config_swap import select_backends_for_mode
from dragon_voice.voice_modes import VoiceMode


@dataclass
class _StubLlmCfg:
    """Minimal subset of LLMConfig used by select_backends_for_mode."""
    backend: str = "ollama"
    local_backend: str = ""
    openrouter_model: str = ""
    ollama_model: str = "ministral-3:3b"
    tinkerclaw_model: str = ""
    system_prompt: str = "(initial)"
    max_tokens: int = 0


@dataclass
class _StubTtsCfg:
    """Minimal subset of TTSConfig used by select_backends_for_mode."""
    backend: str = "kokoro"


@dataclass
class _StubVoiceCfg:
    llm: _StubLlmCfg = field(default_factory=_StubLlmCfg)
    tts: _StubTtsCfg = field(default_factory=_StubTtsCfg)


class SelectBackendsTests(unittest.TestCase):
    # ── LOCAL ────────────────────────────────────────────────────

    def test_local_picks_moonshine_kokoro_ollama(self):
        cfg = _StubVoiceCfg()
        sel = select_backends_for_mode(VoiceMode.LOCAL, cfg)

        self.assertEqual(sel.stt_backend, "moonshine")
        self.assertEqual(sel.tts_backend, "kokoro")
        self.assertEqual(sel.llm_backend, "ollama")
        self.assertEqual(cfg.llm.system_prompt, SYSTEM_PROMPT_LOCAL)
        self.assertEqual(cfg.llm.max_tokens, MAX_TOKENS_LOCAL)

    def test_local_with_plain_model_id_updates_ollama_model(self):
        cfg = _StubVoiceCfg()
        select_backends_for_mode(VoiceMode.LOCAL, cfg, llm_model="gemma3:4b")
        self.assertEqual(cfg.llm.ollama_model, "gemma3:4b")

    def test_local_with_vendor_prefix_rejects_model_update(self):
        """`anthropic/claude-3.5-haiku` is a CLOUD identifier; if it
        somehow lands in a LOCAL config_update, skip the ollama_model
        write rather than corrupt the local config."""
        cfg = _StubVoiceCfg()
        cfg.llm.ollama_model = "ministral-3:3b"
        select_backends_for_mode(
            VoiceMode.LOCAL, cfg,
            llm_model="anthropic/claude-3.5-haiku",
        )
        # The "/" guard at config_swap.py:135 keeps the ollama_model
        # untouched.
        self.assertEqual(cfg.llm.ollama_model, "ministral-3:3b")

    def test_local_with_local_backend_override(self):
        """When `local_backend` is set, use it instead of defaulting
        to ollama (e.g. user configured npu_genie)."""
        cfg = _StubVoiceCfg()
        cfg.llm.local_backend = "npu_genie"
        sel = select_backends_for_mode(VoiceMode.LOCAL, cfg)
        self.assertEqual(sel.llm_backend, "npu_genie")

    # ── HYBRID ───────────────────────────────────────────────────

    def test_hybrid_picks_openrouter_stt_tts_local_llm(self):
        cfg = _StubVoiceCfg()
        sel = select_backends_for_mode(VoiceMode.HYBRID, cfg)

        self.assertEqual(sel.stt_backend, "openrouter")
        self.assertEqual(sel.tts_backend, "openrouter")
        self.assertEqual(sel.llm_backend, "ollama")  # local-tier LLM
        self.assertEqual(cfg.llm.system_prompt, SYSTEM_PROMPT_HYBRID)
        self.assertEqual(cfg.llm.max_tokens, MAX_TOKENS_HYBRID)

    # ── CLOUD ────────────────────────────────────────────────────

    def test_cloud_picks_openrouter_for_all_three(self):
        cfg = _StubVoiceCfg()
        sel = select_backends_for_mode(VoiceMode.CLOUD, cfg)

        self.assertEqual(sel.stt_backend, "openrouter")
        self.assertEqual(sel.tts_backend, "openrouter")
        self.assertEqual(sel.llm_backend, "openrouter")
        self.assertEqual(cfg.llm.system_prompt, SYSTEM_PROMPT_CLOUD)
        self.assertEqual(cfg.llm.max_tokens, MAX_TOKENS_CLOUD)

    def test_cloud_with_llm_model_updates_openrouter_model(self):
        cfg = _StubVoiceCfg()
        select_backends_for_mode(
            VoiceMode.CLOUD, cfg,
            llm_model="anthropic/claude-opus-4.7",
        )
        self.assertEqual(cfg.llm.openrouter_model, "anthropic/claude-opus-4.7")

    # ── TINKERCLAW ───────────────────────────────────────────────

    def test_tinkerclaw_default_uses_local_stt_tts(self):
        cfg = _StubVoiceCfg()
        sel = select_backends_for_mode(VoiceMode.TINKERCLAW, cfg)

        self.assertEqual(sel.stt_backend, "moonshine")
        self.assertEqual(sel.tts_backend, "kokoro")
        self.assertEqual(sel.llm_backend, "tinkerclaw")

    def test_tinkerclaw_with_cloud_suffix_overrides_to_openrouter_stt_tts(self):
        """A llm_model containing 'cloud' (e.g.
        'cloud:claude-sonnet-4.6') flips STT/TTS to OpenRouter while
        keeping TinkerClaw as the LLM gateway.  Matches Tab5's
        voice_mode=3 + cloud-suffix UX."""
        cfg = _StubVoiceCfg()
        sel = select_backends_for_mode(
            VoiceMode.TINKERCLAW, cfg,
            llm_model="cloud:claude-sonnet-4.6",
        )
        self.assertEqual(sel.stt_backend, "openrouter")
        self.assertEqual(sel.tts_backend, "openrouter")
        self.assertEqual(sel.llm_backend, "tinkerclaw")
        self.assertEqual(cfg.llm.tinkerclaw_model, "cloud:claude-sonnet-4.6")

    def test_tinkerclaw_skips_system_prompt_and_max_tokens_override(self):
        """TinkerClaw owns its own personality + token budget — Dragon
        must not stomp the LLMConfig prompts/tokens for TC mode."""
        cfg = _StubVoiceCfg()
        cfg.llm.system_prompt = "(should not be touched)"
        cfg.llm.max_tokens = 999
        select_backends_for_mode(VoiceMode.TINKERCLAW, cfg)
        self.assertEqual(cfg.llm.system_prompt, "(should not be touched)")
        self.assertEqual(cfg.llm.max_tokens, 999)

    def test_tinkerclaw_without_llm_model_does_not_touch_tinkerclaw_model(self):
        cfg = _StubVoiceCfg()
        cfg.llm.tinkerclaw_model = "minimax/MiniMax-M2.5"
        select_backends_for_mode(VoiceMode.TINKERCLAW, cfg, llm_model=None)
        # Field unchanged when no model arg provided.
        self.assertEqual(cfg.llm.tinkerclaw_model, "minimax/MiniMax-M2.5")

    # ── ONBOARD ──────────────────────────────────────────────────

    def test_onboard_falls_through_to_cloud_branch(self):
        """Mode 4 (Onboard) is Tab5-side only — Dragon never sees it
        in normal operation.  But if it does arrive, it falls into the
        else branches: STT/TTS=openrouter, LLM=ollama (local default).
        This pins the existing fall-through behaviour."""
        cfg = _StubVoiceCfg()
        sel = select_backends_for_mode(VoiceMode.ONBOARD, cfg)
        # ONBOARD isn't local/hybrid/cloud/TC — falls into the else
        # of each branch chain.
        self.assertEqual(sel.stt_backend, "openrouter")
        self.assertEqual(sel.tts_backend, "openrouter")
        self.assertEqual(sel.llm_backend, "ollama")
        self.assertEqual(cfg.llm.system_prompt, SYSTEM_PROMPT_CLOUD)
        self.assertEqual(cfg.llm.max_tokens, MAX_TOKENS_CLOUD)

    # ── SOLO (W3-A) ──────────────────────────────────────────────

    def test_solo_mirrors_local_backend_choice(self):
        """Mode 5 (Solo) routes Tab5 directly to OpenRouter — Dragon's
        backends are never invoked.  But we still want sensible
        defaults so a user flipping back to Local mid-session doesn't
        end up with cloud-shaped placeholders.  Mirrors LOCAL on
        STT/TTS/LLM; system_prompt + max_tokens left untouched (Tab5's
        openrouter_client owns the SOLO prompt)."""
        cfg = _StubVoiceCfg()
        sel = select_backends_for_mode(VoiceMode.SOLO, cfg)
        self.assertEqual(sel.stt_backend, "moonshine")
        self.assertEqual(sel.tts_backend, "kokoro")
        self.assertEqual(sel.llm_backend, "ollama")
        # Dragon's system_prompt/max_tokens untouched — SOLO branch
        # is `pass` (Tab5 owns prompts for its direct OpenRouter calls).
        self.assertEqual(cfg.llm.system_prompt, "(initial)")
        self.assertEqual(cfg.llm.max_tokens, 0)

    def test_solo_with_local_backend_override(self):
        """SOLO still respects local_backend so a Dragon configured
        for npu_genie keeps its placeholder LLM choice."""
        cfg = _StubVoiceCfg()
        cfg.llm.local_backend = "npu_genie"
        sel = select_backends_for_mode(VoiceMode.SOLO, cfg)
        self.assertEqual(sel.llm_backend, "npu_genie")


if __name__ == "__main__":
    unittest.main()
