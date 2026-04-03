"""OpenRouter cloud TTS backend.

Sends text to OpenRouter's gpt-audio-mini model with audio modality output,
receives base64-encoded WAV audio response.
"""

import base64
import io
import logging
import os
import wave
from typing import Optional

import aiohttp
import numpy as np

from dragon_voice.config import TTSConfig
from dragon_voice.tts.base import TTSBackend

logger = logging.getLogger(__name__)

MODEL = "openai/gpt-audio-mini"


class OpenRouterTTSBackend(TTSBackend):
    """Cloud TTS via OpenRouter's audio-capable models."""

    def __init__(self, config: TTSConfig) -> None:
        self._config = config
        self._base_url = (config.openrouter_url or "https://openrouter.ai/api/v1").rstrip("/")
        self._api_key = config.openrouter_api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self._voice = config.openrouter_voice or "alloy"
        self._sample_rate_val = 24000  # OpenAI audio models output 24kHz

        self._session: Optional[aiohttp.ClientSession] = None

    async def initialize(self) -> None:
        if not self._api_key:
            raise ValueError(
                "OpenRouter API key required for cloud TTS. "
                "Set llm.openrouter_api_key in config.yaml or OPENROUTER_API_KEY env var."
            )
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30, sock_read=25),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://tinkerclaw.local",
                "X-Title": "TinkerClaw Dragon Voice",
            },
        )
        logger.info("OpenRouter TTS initialized — model=%s, voice=%s", MODEL, self._voice)

    async def synthesize(self, text: str) -> bytes:
        if not self._session or self._session.closed:
            await self.initialize()

        payload = {
            "model": MODEL,
            "messages": [{"role": "user", "content": f"Say exactly the following text aloud: {text}"}],
            "modalities": ["text", "audio"],
            "audio": {"voice": self._voice, "format": "wav"},
        }

        try:
            async with self._session.post(
                f"{self._base_url}/chat/completions", json=payload
            ) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    logger.error("OpenRouter TTS error %d: %s", resp.status, err[:300])
                    return b""
                data = await resp.json()

                # Extract audio from response
                msg = data["choices"][0]["message"]
                audio_data = msg.get("audio", {}).get("data", "")
                if not audio_data:
                    logger.warning("OpenRouter TTS: no audio in response")
                    return b""

                # Decode base64 WAV
                wav_bytes = base64.b64decode(audio_data)

                # Extract raw PCM from WAV
                wav_buf = io.BytesIO(wav_bytes)
                with wave.open(wav_buf, "rb") as wf:
                    self._sample_rate_val = wf.getframerate()
                    pcm_bytes = wf.readframes(wf.getnframes())

                # Convert to int16 numpy array then back to bytes
                # (WAV may be various bit depths, normalize to int16)
                if wf.getsampwidth() == 2:
                    result = pcm_bytes
                elif wf.getsampwidth() == 4:
                    # 32-bit float → int16
                    floats = np.frombuffer(pcm_bytes, dtype=np.float32)
                    result = (floats * 32767).clip(-32768, 32767).astype(np.int16).tobytes()
                else:
                    result = pcm_bytes

                logger.info("OpenRouter TTS: %d bytes @ %dHz for '%.40s...'",
                           len(result), self._sample_rate_val, text)
                return result

        except Exception as e:
            logger.error("OpenRouter TTS request failed: %s", e)
            return b""

    async def shutdown(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        logger.info("OpenRouter TTS shut down")

    @property
    def sample_rate(self) -> int:
        return self._sample_rate_val

    @property
    def name(self) -> str:
        return f"OpenRouter TTS ({MODEL}, {self._voice})"
