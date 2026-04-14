"""OpenRouter cloud STT backend.

Sends base64-encoded WAV audio to OpenRouter's gpt-audio-mini model
for transcription via the chat completions API.
"""

import base64
import io
import logging
import os
import wave
from typing import Optional

import aiohttp

from dragon_voice.config import STTConfig
from dragon_voice.stt.base import STTBackend

logger = logging.getLogger(__name__)

MODEL = "openai/gpt-audio-mini"


class OpenRouterSTTBackend(STTBackend):
    """Cloud STT via OpenRouter's audio-capable models."""

    def __init__(self, config: STTConfig) -> None:
        self._config = config
        self._base_url = (config.openrouter_url or "https://openrouter.ai/api/v1").rstrip("/")
        self._api_key = config.openrouter_api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_usage: dict = {}  # Token usage from last API call
        self.total_calls: int = 0    # Total API calls made

    async def initialize(self) -> None:
        if not self._api_key:
            raise ValueError(
                "OpenRouter API key required for cloud STT. "
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
        logger.info("OpenRouter STT initialized — model=%s", MODEL)

    async def transcribe(self, audio_bytes: bytes, sample_rate: int = 16000) -> str:
        if not self._session or self._session.closed:
            await self.initialize()

        # Wrap raw PCM int16 in a WAV container for the API
        wav_buf = io.BytesIO()
        with wave.open(wav_buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(sample_rate)
            wf.writeframes(audio_bytes)
        wav_bytes = wav_buf.getvalue()
        b64_audio = base64.b64encode(wav_bytes).decode("ascii")

        payload = {
            "model": MODEL,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "You are a speech-to-text transcriber. Transcribe the spoken words in this audio. If there is no speech, output nothing. Output ONLY the transcript with no commentary."},
                    {"type": "input_audio", "input_audio": {"data": b64_audio, "format": "wav"}},
                ],
            }],
        }

        try:
            self.total_calls += 1
            async with self._session.post(
                f"{self._base_url}/chat/completions", json=payload
            ) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    logger.error("OpenRouter STT error %d: %s", resp.status, err[:300])
                    return ""
                data = await resp.json()
                # Defensive parsing — OpenRouter format may vary
                choices = data.get("choices", [])
                if not choices:
                    logger.warning("OpenRouter STT: no choices in response")
                    return ""
                message = choices[0].get("message", {})
                text = (message.get("content") or "").strip()
                # Extract usage for cost tracking
                usage = data.get("usage", {})
                self._last_usage = usage
                logger.info("OpenRouter STT: '%s' (tokens: %s)", text[:80], usage)
                return text
        except Exception as e:
            logger.error("OpenRouter STT request failed: %s", e)
            return ""

    async def shutdown(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        logger.info("OpenRouter STT shut down")

    @property
    def name(self) -> str:
        return f"OpenRouter STT ({MODEL})"
