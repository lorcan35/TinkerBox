"""Supertonic-3 TTS backend.

99M-param ONNX model from Supertone (MIT).  10 built-in voices
(M1-M5 / F1-F5), 10 inline expression tags (``<laugh>``, ``<sigh>``,
``<breath>``, etc.), zero-shot voice cloning via Voice Builder JSON.
Output: 44.1 kHz mono float32 — converted to int16 PCM here.

Runs sub-realtime on ARM Cortex-A78 CPU via onnxruntime.  No GPU
needed.  Model + assets (~400 MB) auto-download into
``~/.cache/supertonic3/`` on first ``initialize()``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import numpy as np

from dragon_voice.config import TTSConfig
from dragon_voice.tts.base import TTSBackend
from dragon_voice.tts.registry import register_tts

logger = logging.getLogger(__name__)

_SUPERTONIC_SR = 44100


@register_tts("supertonic")
class SupertonicBackend(TTSBackend):
    """TTS backend using the supertonic ONNX library."""

    def __init__(self, config: TTSConfig) -> None:
        self._config = config
        self._tts = None
        self._voice_style = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        try:
            from supertonic import TTS as _TTS
        except ImportError as err:
            raise ImportError(
                "supertonic is required for the supertonic backend. "
                "Install it: pip install supertonic"
            ) from err

        _DEFAULT_VOICE = "F1"
        _VALID = {f"{g}{n}" for g in ("F", "M") for n in (1, 2, 3, 4, 5)}
        requested = (
            getattr(self._config, "supertonic_voice", None) or _DEFAULT_VOICE
        )
        if requested not in _VALID:
            logger.warning(
                "Supertonic: voice %r not in valid set %s — falling back to %s",
                requested, sorted(_VALID), _DEFAULT_VOICE,
            )
            voice_name = _DEFAULT_VOICE
        else:
            voice_name = requested
        lang = (
            getattr(self._config, "supertonic_lang", None) or "en"
        )
        logger.info(
            "Initializing Supertonic — voice=%s lang=%s (auto-download)",
            voice_name, lang,
        )

        loop = asyncio.get_running_loop()

        def _load():
            t = _TTS(auto_download=True)
            style = t.get_voice_style(voice_name=voice_name)
            return t, style

        try:
            self._tts, self._voice_style = await loop.run_in_executor(
                None, _load
            )
            self._lang = lang
            logger.info("Supertonic loaded successfully")
        except Exception:
            logger.exception("Failed to initialize Supertonic")
            raise

    async def synthesize(self, text: str) -> bytes:
        """Synthesize to PCM int16 mono at 44.1 kHz."""
        if not text.strip():
            return b""
        if self._tts is None:
            raise RuntimeError("SupertonicBackend not initialized")

        from dragon_voice.tts.text_cleaner import clean_for_tts
        cleaned = clean_for_tts(text)
        text = cleaned or text

        speed = float(getattr(self._config, "supertonic_speed", None) or 1.0)
        steps = int(getattr(self._config, "supertonic_steps", None) or 8)

        async with self._lock:
            loop = asyncio.get_running_loop()

            def _synthesize():
                wav, _ = self._tts.synthesize(
                    text=text,
                    voice_style=self._voice_style,
                    lang=self._lang,
                    total_steps=steps,
                    speed=speed,
                )
                # wav: float32 in [-1, 1], possibly (1, N) — squeeze + i16
                arr = np.asarray(wav).squeeze().astype(np.float32)
                pcm = (arr * 32767.0).clip(-32768, 32767).astype(np.int16)
                return pcm.tobytes()

            try:
                pcm = await loop.run_in_executor(None, _synthesize)
            except Exception:
                logger.exception("Supertonic synthesis failed")
                return b""

        logger.debug(
            "Supertonic synthesized %d bytes for: %.40s...", len(pcm), text
        )
        return pcm

    async def shutdown(self) -> None:
        self._tts = None
        self._voice_style = None
        logger.info("Supertonic shut down")

    @property
    def sample_rate(self) -> int:
        return _SUPERTONIC_SR

    @property
    def name(self) -> str:
        voice = getattr(self._config, "supertonic_voice", None) or "F1"
        return f"Supertonic ({voice})"
