"""OpenRouter cloud TTS backend.

Sends text to OpenRouter's gpt-audio-mini model with audio modality,
streams pcm16 audio chunks via SSE, returns raw PCM int16 at 24kHz.
"""

import base64
import json
import logging
import os
from typing import Optional

import aiohttp

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
        self._sample_rate_val = 24000  # OpenAI audio models output 24kHz pcm16

        self._session: Optional[aiohttp.ClientSession] = None
        self.total_calls: int = 0    # Total API calls made

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

        # Must use stream=True + format=pcm16 (wav only works non-streaming)
        payload = {
            "model": MODEL,
            "messages": [{"role": "user", "content": f"Say exactly: {text}"}],
            "modalities": ["text", "audio"],
            "audio": {"voice": self._voice, "format": "pcm16"},
            "stream": True,
        }

        try:
            self.total_calls += 1
            async with self._session.post(
                f"{self._base_url}/chat/completions", json=payload
            ) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    logger.error("OpenRouter TTS error %d: %s", resp.status, err[:300])
                    return b""

                # Collect base64 pcm16 chunks from SSE stream
                audio_b64_parts = []
                async for line in resp.content:
                    line = line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        audio_data = delta.get("audio", {}).get("data", "")
                        if audio_data:
                            audio_b64_parts.append(audio_data)
                    except json.JSONDecodeError:
                        continue

                if not audio_b64_parts:
                    logger.warning("OpenRouter TTS: no audio chunks received")
                    return b""

                # Decode all base64 chunks into raw PCM int16
                pcm_bytes = base64.b64decode("".join(audio_b64_parts))
                duration = len(pcm_bytes) / 2 / self._sample_rate_val
                logger.info("OpenRouter TTS: %d bytes (%.1fs @ %dHz) for '%.40s...'",
                           len(pcm_bytes), duration, self._sample_rate_val, text)
                return pcm_bytes

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
