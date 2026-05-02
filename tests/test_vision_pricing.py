"""Tests for ``vision_per_frame_mils`` (OCP-2, audit 2026-05-03).

The prior inline switch in ``server.py:_handle_config_update`` hardcoded
per-frame mils for ``gpt-4o``/``sonnet``/``haiku``/``gemini`` and
silently defaulted everything else (Opus 4.x, Grok 4.x, Kimi K2.6,
Qwen 3.6, GLM, MiMo) to 0 — meaning every cloud-vision turn on those
models was invisible to the daily-cap budget enforcement.

These tests pin the new centralized helper:
  - Local models (no slash) cost zero, never the ``_default`` fallback.
  - Models in ``_PRICING_MILS_PER_M`` get real per-frame mils.
  - Models NOT in the registry fall through to ``_default`` (~3000 mils
    for the ~$2/M-token conservative default), never zero.
  - All the previously-silently-zero models now round-trip through the
    canonical pricing table.

Run:
    python3 -m pytest -v tests/test_vision_pricing.py
"""
from __future__ import annotations

import unittest

from dragon_voice.llm.openrouter_llm import (
    _PRICING_MILS_PER_M,
    _VISION_TOKENS_PER_FRAME_APPROX,
    price_for_model,
    vision_per_frame_mils,
)


class VisionPerFrameMilsTests(unittest.TestCase):
    def test_local_model_no_slash_returns_zero(self):
        """Local on-device models cost zero marginal USD."""
        for local_id in (
            "ministral-3:3b",
            "gemma3:4b",
            "qwen3:1.7b",
            "llama3.2:3b",
            "hf.co/openbmb/MiniCPM-V-4-gguf:Q4_K_M",  # local Ollama, has slashes
        ):
            with self.subTest(model=local_id):
                # Note: the "hf.co/..." case has slashes but is hosted
                # locally — price_for_model decides only by presence of
                # ANY slash.  This matches existing semantics; if we
                # ever care, the fix is to look at the model's caps.tier
                # rather than its id.  For now, document the behavior.
                if "/" not in local_id:
                    self.assertEqual(vision_per_frame_mils(local_id), 0)

    def test_empty_model_returns_zero(self):
        self.assertEqual(vision_per_frame_mils(""), 0)

    def test_known_cloud_models_get_real_mils(self):
        """Each canonical pricing entry must produce a non-zero mils
        cost for vision frames."""
        for model in (
            "openai/gpt-4o",
            "openai/gpt-4o-mini",
            "anthropic/claude-sonnet-4.6",
            "anthropic/claude-3.5-haiku",
            "anthropic/claude-haiku-4.5",
            "google/gemini-3-flash-preview",
            "google/gemini-3.1-pro-preview",
        ):
            with self.subTest(model=model):
                cost = vision_per_frame_mils(model)
                self.assertGreater(
                    cost, 0,
                    f"{model} must cost > 0 mils per vision frame",
                )

    def test_previously_silently_zero_models_now_priced(self):
        """OCP-2 anchor: these are exactly the models the prior inline
        switch silently defaulted to 0 mils.  All must now produce
        real per-frame mils via the canonical pricing table."""
        previously_silent = [
            "anthropic/claude-opus-4.7",
            "anthropic/claude-opus-4.6",
            "anthropic/claude-opus-4.5",
            "x-ai/grok-4.20",
            "x-ai/grok-4.20-multi-agent",
            "moonshotai/kimi-k2.6",
            "moonshotai/kimi-k2.5",
            "qwen/qwen3.6-flash",
            "qwen/qwen3.6-27b",
            "z-ai/glm-5.1",
            "z-ai/glm-5v-turbo",
            "xiaomi/mimo-v2.5",
        ]
        for model in previously_silent:
            with self.subTest(model=model):
                cost = vision_per_frame_mils(model)
                self.assertGreater(
                    cost, 0,
                    f"OCP-2 regression: {model} must NOT silently zero — "
                    f"prior switch defaulted everything except gpt-4o/sonnet/"
                    f"haiku/gemini to 0 mils per frame, hiding spend from "
                    f"the daily cap.",
                )

    def test_unknown_cloud_model_falls_through_to_default(self):
        """An OpenRouter-shape id (vendor/model) that isn't in the
        registry must use the conservative ``_default`` rate, never
        zero — pricing surprises are always errors-on-the-side-of-too-
        much, never errors-on-the-side-of-free."""
        cost = vision_per_frame_mils("future-vendor/unreleased-model-2027")
        # Default is $2/M tokens input → 2_000_000 mils/M × 1500 tokens
        # ÷ 1_000_000 (ceil) = 3000 mils.
        self.assertEqual(cost, 3000)

    def test_helper_matches_price_for_model_call_shape(self):
        """The helper is just sugar over ``price_for_model`` with a
        fixed token approximation — assert the math agrees so a future
        change to either side stays consistent."""
        for model in (
            "anthropic/claude-sonnet-4.6",
            "anthropic/claude-opus-4.7",
            "google/gemini-3-flash-preview",
        ):
            with self.subTest(model=model):
                via_helper = vision_per_frame_mils(model)
                direct = price_for_model(
                    model,
                    prompt_tokens=_VISION_TOKENS_PER_FRAME_APPROX,
                    completion_tokens=0,
                )
                self.assertEqual(via_helper, direct)

    def test_pricing_table_has_no_silent_zeroes(self):
        """Belt-and-suspenders: every entry in _PRICING_MILS_PER_M must
        produce a non-zero per-frame cost.  If anyone adds a registry
        entry with zero input mils, this test catches it before it
        ships."""
        for model, entry in _PRICING_MILS_PER_M.items():
            if model == "_default":
                continue
            with self.subTest(model=model):
                self.assertGreater(
                    entry.get("in", 0), 0,
                    f"{model}: 'in' mils per M tokens must be > 0",
                )
                self.assertGreater(vision_per_frame_mils(model), 0)


if __name__ == "__main__":
    unittest.main()
