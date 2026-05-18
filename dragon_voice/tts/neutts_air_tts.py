"""NeuTTS Air backend — Neuphonic's 748M speech-LM with voice cloning.

Uses the Q4_0 GGUF for the backbone (via llama-cpp-python) + the
NeuCodec decoder (PyTorch).  Voice cloning works by encoding a
reference audio clip + reference transcript once at startup; every
subsequent synthesis inherits the timbre and prosody of that clip.

The reference WE ship by default is a 2.9 s CSM-1B "Tinker" voice
sample generated overnight — that's how we keep the CSM voice quality
on Dragon at ~6× realtime instead of CSM's own 131× realtime.

Latency on Q6A (Cortex-A78×4 cluster, 4 threads):
  - First-load: ~30-45 s (GGUF mmap + NeuCodec PyTorch init + ref encode)
  - Per-utterance: ~6× realtime — a 3 s reply takes ~18 s to render.
    Use this as the "premium voice" backend; fall back to Kokoro/Piper
    for snappy live conversation.

Outputs 16-bit PCM at 24 kHz mono, same as Kokoro.
"""

import asyncio
import logging
from pathlib import Path

import numpy as np

from dragon_voice.config import TTSConfig
from dragon_voice.tts.base import TTSBackend
from dragon_voice.tts.registry import register_tts

logger = logging.getLogger(__name__)

_NEUTTS_SAMPLE_RATE = 24000
_DEFAULT_REF_AUDIO = "/home/radxa/csm/hello.wav"
_DEFAULT_REF_TEXT = "Hello, Emile. I'm Tinker. Nice to meet you."


@register_tts("neutts_air")
class NeuTTSAirBackend(TTSBackend):
    """Voice-cloning TTS via NeuTTS Air Q4 GGUF + NeuCodec."""

    def __init__(self, config: TTSConfig) -> None:
        self._config = config
        self._tts = None
        self._ref_codes = None
        self._ref_text = _DEFAULT_REF_TEXT
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        try:
            from neuttsair.neutts import NeuTTSAir
        except ImportError as err:
            raise ImportError(
                "neutts package missing.  Install with: "
                "pip install 'neutts[llama]' (see TinkerBox docs/PLAN-tinkerbox-integrations.md)"
            ) from err

        ref_audio = getattr(self._config, "neutts_ref_audio", "") or _DEFAULT_REF_AUDIO
        ref_text = getattr(self._config, "neutts_ref_text", "") or _DEFAULT_REF_TEXT
        if not Path(ref_audio).exists():
            raise FileNotFoundError(
                f"NeuTTS reference audio missing: {ref_audio}.  Generate via the "
                "CSM bench (~/csm/hello.wav) or point neutts_ref_audio at a 3-15 s "
                "clean clip of the voice you want Tinker to inherit."
            )

        self._ref_text = ref_text
        logger.info("Initializing NeuTTS Air — ref=%s", ref_audio)

        loop = asyncio.get_running_loop()

        def _load_and_encode():
            tts = NeuTTSAir(
                backbone_repo="neuphonic/neutts-air-q4-gguf",
                backbone_device="cpu",
                codec_repo="neuphonic/neucodec",
                codec_device="cpu",
            )
            codes = tts.encode_reference(ref_audio)
            return tts, codes

        try:
            self._tts, self._ref_codes = await loop.run_in_executor(None, _load_and_encode)
            logger.info("NeuTTS Air ready (voice cloned from %s)", Path(ref_audio).name)
        except Exception:
            logger.exception("NeuTTS Air init failed")
            raise

    async def synthesize(self, text: str) -> bytes:
        if not text.strip():
            return b""
        if self._tts is None or self._ref_codes is None:
            raise RuntimeError("NeuTTSAirBackend not initialized")

        from dragon_voice.tts.text_cleaner import clean_for_tts

        cleaned = clean_for_tts(text)
        if cleaned and cleaned != text:
            logger.info(
                "NeuTTS cleaner: %d→%d chars (in=%r out=%r)",
                len(text), len(cleaned), text[:80], cleaned[:80],
            )
        text_to_synth = cleaned or text

        async with self._lock:
            loop = asyncio.get_running_loop()

            def _synthesize():
                wav_f32 = self._tts.infer(text_to_synth, self._ref_codes, self._ref_text)
                # neuttsair returns float32 numpy at 24 kHz; convert to int16
                wav_i16 = np.clip(wav_f32 * 32767, -32768, 32767).astype(np.int16)
                return wav_i16.tobytes()

            try:
                return await loop.run_in_executor(None, _synthesize)
            except Exception:
                logger.exception("NeuTTS synthesis failed")
                return b""

    async def shutdown(self) -> None:
        # NeuTTSAir holds a Llama handle that auto-closes on GC; no explicit
        # cleanup needed beyond dropping references.
        self._tts = None
        self._ref_codes = None

    @property
    def sample_rate(self) -> int:
        return _NEUTTS_SAMPLE_RATE

    @property
    def name(self) -> str:
        return "neutts_air"
