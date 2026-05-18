"""Kokoro TTS backend using kokoro-onnx.

Kokoro is a high-quality neural TTS model with small footprint,
suitable for on-device synthesis. Outputs at 24000 Hz.
"""

import asyncio
import logging
from pathlib import Path

import numpy as np

from dragon_voice.config import TTSConfig
from dragon_voice.tts.base import TTSBackend
from dragon_voice.tts.registry import register_tts

logger = logging.getLogger(__name__)

_CACHE_DIR = Path.home() / ".cache" / "dragon_voice" / "kokoro"
_KOKORO_SAMPLE_RATE = 24000


@register_tts("kokoro")
class KokoroBackend(TTSBackend):
    """TTS backend using kokoro-onnx."""

    def __init__(self, config: TTSConfig) -> None:
        self._config = config
        self._kokoro = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Load the Kokoro model."""
        # Compat shim for kokoro-onnx 0.5.0 + phonemizer-fork ≥ 3.3:
        # (a) restore the static `EspeakWrapper.set_data_path` method
        #     (became a `data_path` property in 3.3).
        # (b) IGNORE whatever path kokoro hands us — it comes from
        #     `espeakng_loader.get_data_path()` which is baked in at
        #     wheel-build time and points at the CI runner's tmpdir
        #     (`/home/runner/work/espeakng-loader/…`).  Force the
        #     system espeak-ng-data dir, which DOES exist.
        try:
            from phonemizer.backend.espeak.wrapper import EspeakWrapper
            _SYS_ESPEAK_DATA = "/usr/lib/aarch64-linux-gnu/espeak-ng-data"
            if Path(_SYS_ESPEAK_DATA).is_dir():
                def _set_data_path(_ignored):
                    EspeakWrapper.data_path = _SYS_ESPEAK_DATA
                EspeakWrapper.set_data_path = staticmethod(_set_data_path)
                EspeakWrapper.data_path = _SYS_ESPEAK_DATA
            elif not hasattr(EspeakWrapper, "set_data_path"):
                def _set_data_path(path):
                    EspeakWrapper.data_path = path
                EspeakWrapper.set_data_path = staticmethod(_set_data_path)
        except Exception as patch_err:
            logger.warning("phonemizer compat shim skipped: %s", patch_err)

        try:
            import kokoro_onnx
        except ImportError as err:
            raise ImportError(
                "kokoro-onnx is required for the kokoro backend. "
                "Install it: pip install kokoro-onnx"
            ) from err

        # #338: kokoro-onnx 0.5.0+ wants (model_path, voices_path)
        # explicitly.  Default to the well-known cache location so a
        # vanilla deploy can find the files dropped by the install
        # script — see docs / TT #564 deploy notes.
        model_path = self._config.kokoro_model_path or str(
            _CACHE_DIR / "kokoro-v1.0.onnx"
        )
        voices_path = self._config.kokoro_voices_path or str(
            _CACHE_DIR / "voices-v1.0.bin"
        )
        voice = self._config.kokoro_voice

        if not Path(model_path).exists() or not Path(voices_path).exists():
            raise FileNotFoundError(
                f"Kokoro model files missing — expected "
                f"model={model_path} voices={voices_path}.  Download "
                f"from github.com/thewh1teagle/kokoro-onnx/releases."
            )

        logger.info(
            "Initializing Kokoro TTS — voice=%s, model=%s",
            voice, model_path,
        )

        loop = asyncio.get_running_loop()

        def _load():
            return kokoro_onnx.Kokoro(model_path, voices_path)

        try:
            self._kokoro = await loop.run_in_executor(None, _load)
            logger.info("Kokoro TTS loaded successfully")
        except Exception:
            logger.exception("Failed to initialize Kokoro TTS")
            raise

    async def synthesize(self, text: str) -> bytes:
        """Synthesize text to PCM int16 audio bytes at 24kHz."""
        if not text.strip():
            return b""

        if self._kokoro is None:
            raise RuntimeError("KokoroBackend not initialized")

        # #338 defense-in-depth: clean here too, in case a caller
        # forgot.  Idempotent — running on already-cleaned text yields
        # the same string.  Emits a single INFO log per call with the
        # before/after delta so we can grep for it.
        from dragon_voice.tts.text_cleaner import clean_for_tts
        cleaned = clean_for_tts(text)
        if cleaned and cleaned != text:
            logger.info(
                "#338 Kokoro cleaner: %d→%d chars (in=%r out=%r)",
                len(text), len(cleaned), text[:80], cleaned[:80],
            )
        text = cleaned or text

        voice = self._config.kokoro_voice

        async with self._lock:
            loop = asyncio.get_running_loop()

            def _synthesize():
                # kokoro-onnx returns (audio_float32, sample_rate)
                audio_f32, sr = self._kokoro.create(
                    text,
                    voice=voice,
                    speed=1.0,
                )
                # Convert float32 [-1, 1] to int16
                audio_i16 = (audio_f32 * 32767).clip(-32768, 32767).astype(np.int16)
                return audio_i16.tobytes()

            try:
                pcm = await loop.run_in_executor(None, _synthesize)
            except Exception:
                logger.exception("Kokoro synthesis failed")
                return b""

        logger.debug("Kokoro synthesized %d bytes for: %.40s...", len(pcm), text)
        return pcm

    async def shutdown(self) -> None:
        self._kokoro = None
        logger.info("Kokoro TTS shut down")

    @property
    def sample_rate(self) -> int:
        return _KOKORO_SAMPLE_RATE

    @property
    def name(self) -> str:
        return f"Kokoro ({self._config.kokoro_voice})"
