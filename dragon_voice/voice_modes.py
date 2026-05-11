"""Voice-mode registry — centralizes the six-tier mode semantics.

2026-05-03 SOLID audit (OCP-1, OCP-2, OCP-6): server.py carried 17
``voice_mode == N`` magic-number comparisons scattered across
``_handle_config_update`` and ``_handle_text_body``.  Adding a new
mode meant hunting all 17.  The vision-pricing OCP-2 bug fixed in
PR #210 was an instance of the same shape: a switch-on-substring
that silently zeroed when a new model arrived.

This module replaces the magic numbers with a typed
:class:`VoiceMode` IntEnum + the semantic predicates the code
actually wants to ask:

  * ``is_local()`` / ``is_hybrid()`` / ``is_cloud()`` /
    ``is_tinkerclaw()`` / ``is_onboard()`` / ``is_solo()`` —
    single-tier checks
  * ``needs_cloud_stt_tts()`` — Hybrid + Cloud both route STT+TTS
    through OpenRouter.  This was the ``voice_mode in (1, 2)``
    pattern that appeared at server.py:2240 and 2310.
  * ``needs_openrouter_key()`` — currently the same set as
    ``needs_cloud_stt_tts()`` but kept separate so a future change
    (e.g. local STT + cloud LLM) doesn't have to re-derive.
  * ``is_dragon_managed_pipeline()`` — Local / Hybrid / Cloud all run
    Dragon's STT→LLM→TTS chain.  TinkerClaw bypasses to the gateway;
    Onboard and Solo are Tab5-side and Dragon never drives the turn.

The IntEnum subtype keeps wire-protocol compatibility — JSON ints
from Tab5 still serialise straight through.  ``VoiceMode(0)`` and
``int(VoiceMode.LOCAL)`` round-trip via the existing JSON layer
without any adapter changes.

For unparseable WS payloads (out-of-range int, missing field), use
:meth:`VoiceMode.from_int` — it returns ``None`` instead of raising
``ValueError`` so callers can fall back gracefully.

Wave 3-A (TT cross-stack audit 2026-05-11): added ``SOLO = 5`` so
the firmware's vmode=5 (Tab5 → OpenRouter direct, no Dragon in the
audio path) round-trips through ``from_int`` instead of falling
through the unknown-value branch and silently downgrading to LOCAL.
"""
from __future__ import annotations

from enum import IntEnum
from typing import Optional


class VoiceMode(IntEnum):
    """Five-tier voice mode controlling Dragon's STT/LLM/TTS routing.

    Wire values match what Tab5 firmware sends in the
    ``config_update`` WS frame.  Don't reorder — the integer values
    are part of the protocol contract (see TinkerTab CLAUDE.md
    "Voice Modes").
    """

    LOCAL = 0
    """Moonshine STT + local NPU/Ollama LLM + Piper TTS, all on Dragon."""

    HYBRID = 1
    """OpenRouter STT/TTS + local LLM (gpt-audio-mini for STT/TTS,
    Dragon's local backend for the LLM turn)."""

    CLOUD = 2
    """OpenRouter STT + user-selected cloud LLM + OpenRouter TTS.
    The LLM model id comes from the WS frame's ``llm_model`` field."""

    TINKERCLAW = 3
    """TinkerClaw gateway handles the LLM turn (Dragon is just an
    audio pipe).  STT/TTS can be either local Moonshine/Piper or
    OpenRouter depending on the per-mode config."""

    ONBOARD = 4
    """K144 onboard LLM, Tab5-side only.  Dragon never sees this
    mode on the wire — Tab5 silently maps it to LOCAL when sending
    ``config_update`` so Dragon's STT/TTS still run.  Included here
    for completeness so :meth:`from_int` recognises 4 as a valid
    enum value rather than ``None``."""

    SOLO = 5
    """SOLO_DIRECT — Tab5 → OpenRouter directly, no Dragon in the
    audio path (TT #370, shipped 2026-05-11).  Dragon's pipeline is
    idle during SOLO turns; backends aren't touched.  Wave 3-B will
    make Tab5 send vmode=5 on the wire instead of short-circuiting
    config_update (which it does today to avoid the pre-W3-A
    ``from_int(5) → None → downgrade-to-LOCAL`` fallthrough)."""

    @classmethod
    def from_int(cls, value: Optional[int]) -> Optional["VoiceMode"]:
        """Safe parse from a raw int (e.g. WS frame field).

        Returns ``None`` for ``None``, out-of-range values, or
        non-int types.  Use this at boundaries where the input is
        untrusted; use the enum directly for in-tree code paths
        that should never see an invalid value.
        """
        if value is None:
            return None
        try:
            return cls(int(value))
        except (ValueError, TypeError):
            return None

    # ── single-tier predicates ─────────────────────────────────

    def is_local(self) -> bool:
        return self == VoiceMode.LOCAL

    def is_hybrid(self) -> bool:
        return self == VoiceMode.HYBRID

    def is_cloud(self) -> bool:
        return self == VoiceMode.CLOUD

    def is_tinkerclaw(self) -> bool:
        return self == VoiceMode.TINKERCLAW

    def is_onboard(self) -> bool:
        return self == VoiceMode.ONBOARD

    def is_solo(self) -> bool:
        return self == VoiceMode.SOLO

    # ── semantic groupings (the real OCP wins) ────────────────

    def needs_cloud_stt_tts(self) -> bool:
        """True iff Dragon's STT and TTS should run on OpenRouter
        rather than local Moonshine + Piper.

        Pre-extract this was ``voice_mode in (1, 2)`` at
        server.py:2240 and ``voice_mode in (1, 2) or
        (voice_mode == 3 and stt_be == "openrouter")`` at 2310.
        """
        return self in (VoiceMode.HYBRID, VoiceMode.CLOUD)

    def needs_openrouter_key(self) -> bool:
        """True iff this mode requires a configured OpenRouter API
        key.  Currently the same set as :meth:`needs_cloud_stt_tts`
        — kept separate so a future "local STT + cloud LLM" tier
        doesn't force a search-and-replace."""
        return self.needs_cloud_stt_tts()

    def is_dragon_managed_pipeline(self) -> bool:
        """True iff Dragon's STT→LLM→TTS pipeline drives the turn.

        Local, Hybrid, Cloud all do.  TinkerClaw bypasses to the
        gateway (Dragon is just an audio pipe).  Onboard runs on
        the Tab5-stacked K144 module.  Solo routes Tab5 directly to
        OpenRouter (no Dragon in the audio path at all).
        """
        return self in (VoiceMode.LOCAL, VoiceMode.HYBRID, VoiceMode.CLOUD)
