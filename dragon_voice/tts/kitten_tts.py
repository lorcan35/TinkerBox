"""KittenTTS backend.

~25M-param ONNX model (Apache-2.0) with 8 expression-named voices
(``expr-voice-2-m``, ``expr-voice-2-f``, ``expr-voice-3-m``, etc.).
English-only, 24 kHz.

Lightweight English fast-path — much smaller than Supertonic /
Kokoro, runs comfortably on ARM Cortex-A78 CPU via onnxruntime.
Depends on espeak-ng-data (already installed system-wide on Dragon
for Kokoro — same symlink shim works).
"""

from __future__ import annotations

import asyncio
import logging

import numpy as np

from dragon_voice.config import TTSConfig
from dragon_voice.tts.base import TTSBackend
from dragon_voice.tts.registry import register_tts

logger = logging.getLogger(__name__)

_KITTEN_SR = 24000


@register_tts("kitten")
class KittenBackend(TTSBackend):
    """TTS backend using the kittentts ONNX library."""

    def __init__(self, config: TTSConfig) -> None:
        self._config = config
        self._tts = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        # Kokoro's espeak-ng compat shim (installed earlier) also
        # benefits KittenTTS — same phonemizer code path.
        try:
            from phonemizer.backend.espeak.wrapper import EspeakWrapper
            from pathlib import Path
            _SYS_ESPEAK = "/usr/lib/aarch64-linux-gnu/espeak-ng-data"
            if Path(_SYS_ESPEAK).is_dir():
                def _set_data_path(_ignored):
                    EspeakWrapper.data_path = _SYS_ESPEAK
                EspeakWrapper.set_data_path = staticmethod(_set_data_path)
                EspeakWrapper.data_path = _SYS_ESPEAK
        except Exception as patch_err:
            logger.warning("phonemizer compat shim skipped: %s", patch_err)

        try:
            from kittentts import KittenTTS as _KittenTTS
        except ImportError as err:
            raise ImportError(
                "kittentts is required for the kitten backend. "
                "Install it: pip install kittentts"
            ) from err

        _DEFAULT = "expr-voice-2-f"
        _VALID = {
            f"expr-voice-{n}-{g}" for n in (2, 3, 4, 5) for g in ("m", "f")
        }
        requested = (
            getattr(self._config, "kitten_voice", None) or _DEFAULT
        )
        if requested not in _VALID:
            logger.warning(
                "KittenTTS: voice %r not in valid set %s — falling back to %s",
                requested, sorted(_VALID), _DEFAULT,
            )
            voice = _DEFAULT
        else:
            voice = requested
        logger.info("Initializing KittenTTS — voice=%s", voice)

        loop = asyncio.get_running_loop()

        def _load():
            return _KittenTTS()

        try:
            self._tts = await loop.run_in_executor(None, _load)
            self._voice = voice
            logger.info("KittenTTS loaded successfully")
        except Exception:
            logger.exception("Failed to initialize KittenTTS")
            raise

    async def synthesize(self, text: str) -> bytes:
        """Synthesize to PCM int16 mono at 24 kHz."""
        if not text.strip():
            return b""
        if self._tts is None:
            raise RuntimeError("KittenBackend not initialized")

        from dragon_voice.tts.text_cleaner import clean_for_tts
        cleaned = clean_for_tts(text)
        text = cleaned or text

        speed = float(getattr(self._config, "kitten_speed", None) or 1.0)

        async with self._lock:
            loop = asyncio.get_running_loop()

            def _synthesize():
                audio = self._tts.generate(
                    text=text, voice=self._voice, speed=speed
                )
                arr = np.asarray(audio).squeeze().astype(np.float32)
                # KittenTTS emits float in roughly [-1, 1].  Clip + int16.
                pcm = (arr * 32767.0).clip(-32768, 32767).astype(np.int16)
                return pcm.tobytes()

            try:
                pcm = await loop.run_in_executor(None, _synthesize)
            except Exception:
                logger.exception("KittenTTS synthesis failed")
                return b""

        logger.debug(
            "KittenTTS synthesized %d bytes for: %.40s...", len(pcm), text
        )
        return pcm

    async def shutdown(self) -> None:
        self._tts = None
        logger.info("KittenTTS shut down")

    @property
    def sample_rate(self) -> int:
        return _KITTEN_SR

    @property
    def name(self) -> str:
        voice = getattr(self._config, "kitten_voice", None) or "expr-voice-2-f"
        return f"KittenTTS ({voice})"
