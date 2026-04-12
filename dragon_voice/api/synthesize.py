"""TTS synthesis and STT transcription API routes."""

import base64
import json
import logging
import time

from aiohttp import web

from dragon_voice.api.utils import json_error, parse_json_body
from dragon_voice.config import VoiceConfig
from dragon_voice.stt import create_stt, STTBackend
from dragon_voice.tts import create_tts, TTSBackend

logger = logging.getLogger(__name__)


class SynthesizeRoutes:
    def __init__(self, voice_config: VoiceConfig) -> None:
        self._config = voice_config
        self._stt: STTBackend | None = None
        self._tts: TTSBackend | None = None

    def register(self, app: web.Application) -> None:
        app.router.add_post("/api/v1/transcribe", self.transcribe_audio)
        app.router.add_post("/api/v1/synthesize", self.synthesize)
        # OTA firmware
        app.router.add_get("/api/ota/check", self.ota_check)
        app.router.add_get("/api/ota/firmware.bin", self.ota_firmware)

    async def _ensure_stt(self) -> STTBackend | None:
        if self._stt:
            return self._stt
        self._stt = create_stt(self._config.stt)
        await self._stt.initialize()
        logger.info("STT initialized for API: %s", self._stt.name)
        return self._stt

    async def _ensure_tts(self) -> TTSBackend | None:
        if self._tts:
            return self._tts
        self._tts = create_tts(self._config.tts)
        await self._tts.initialize()
        logger.info("TTS initialized for API: %s", self._tts.name)
        return self._tts

    async def transcribe_audio(self, request: web.Request) -> web.Response:
        """POST /api/v1/transcribe — raw PCM or WAV → text"""
        stt = await self._ensure_stt()
        if not stt:
            return json_error("STT backend not available", 503)

        sample_rate = int(request.headers.get("X-Sample-Rate", "16000"))
        audio_bytes = await request.read()
        if not audio_bytes or len(audio_bytes) < 100:
            return json_error("No audio data in request body")

        # Strip WAV header if present
        if len(audio_bytes) > 4 and audio_bytes[:4] == b"RIFF":
            data_pos = audio_bytes.find(b"data")
            if data_pos >= 0 and data_pos + 8 <= len(audio_bytes):
                audio_bytes = audio_bytes[data_pos + 8:]
            else:
                audio_bytes = audio_bytes[44:]

        duration_s = len(audio_bytes) / (sample_rate * 2)
        try:
            t0 = time.monotonic()
            transcript = await stt.transcribe(audio_bytes, sample_rate)
            stt_ms = (time.monotonic() - t0) * 1000
            return web.json_response({
                "text": transcript.strip(),
                "duration_s": round(duration_s, 1),
                "stt_ms": round(stt_ms),
            })
        except Exception as e:
            logger.exception("Transcription failed")
            return json_error(f"Transcription failed: {e}", 500)

    async def synthesize(self, request: web.Request) -> web.Response:
        """POST /api/v1/synthesize — text → audio

        Request: {"text": "Hello", "sample_rate": 16000}
        Response: raw PCM bytes (application/octet-stream) or JSON with base64
        """
        body, err = await parse_json_body(request)
        if err:
            return err

        text = body.get("text", "").strip()
        if not text:
            return json_error("'text' field is required")

        target_rate = body.get("sample_rate", self._config.audio.input_sample_rate)

        tts = await self._ensure_tts()
        if not tts:
            return json_error("TTS backend not available", 503)

        try:
            t0 = time.monotonic()
            audio_bytes = await tts.synthesize(text)
            tts_ms = (time.monotonic() - t0) * 1000

            if not audio_bytes:
                return json_error("TTS produced no audio", 500)

            # Resample if needed
            tts_rate = tts.sample_rate
            if tts_rate != target_rate:
                import numpy as np
                audio_i16 = np.frombuffer(audio_bytes, dtype=np.int16)
                ratio = target_rate / tts_rate
                new_len = int(len(audio_i16) * ratio)
                indices = np.arange(new_len) / ratio
                idx_floor = np.clip(indices.astype(np.int32), 0, len(audio_i16) - 2)
                frac = indices - idx_floor
                audio_bytes = (audio_i16[idx_floor] * (1 - frac)
                             + audio_i16[idx_floor + 1] * frac).astype(np.int16).tobytes()

            duration_s = len(audio_bytes) / (target_rate * 2)

            # Check Accept header — JSON or binary
            accept = request.headers.get("Accept", "")
            if "application/json" in accept:
                return web.json_response({
                    "audio_base64": base64.b64encode(audio_bytes).decode(),
                    "sample_rate": target_rate,
                    "duration_s": round(duration_s, 2),
                    "tts_ms": round(tts_ms),
                })

            return web.Response(
                body=audio_bytes,
                content_type="application/octet-stream",
                headers={
                    "X-Sample-Rate": str(target_rate),
                    "X-Duration-Seconds": str(round(duration_s, 2)),
                    "X-TTS-Ms": str(round(tts_ms)),
                    "X-TTS-Backend": tts.name,
                },
            )
        except Exception as e:
            logger.exception("Synthesis failed")
            return json_error(f"Synthesis failed: {e}", 500)

    # ── OTA ──

    OTA_DIR = "/home/radxa/ota"
    OTA_VERSION_FILE = "/home/radxa/ota/version.json"

    async def ota_check(self, request: web.Request) -> web.Response:
        """GET /api/ota/check?current=VERSION"""
        import os
        current = request.query.get("current", "0.0.0")

        if not os.path.exists(self.OTA_VERSION_FILE):
            return web.json_response({"update": False, "current": current})

        try:
            with open(self.OTA_VERSION_FILE) as f:
                info = json.load(f)
        except Exception:
            return web.json_response({"update": False, "current": current})

        available_ver = info.get("version", "0.0.0")
        sha256 = info.get("sha256", "")

        if available_ver <= current:
            return web.json_response({"update": False, "current": current, "available": available_ver})

        host = request.host
        scheme = request.scheme
        firmware_url = f"{scheme}://{host}/api/ota/firmware.bin"
        return web.json_response({
            "update": True, "version": available_ver,
            "url": firmware_url, "sha256": sha256,
        })

    async def ota_firmware(self, request: web.Request) -> web.StreamResponse:
        """GET /api/ota/firmware.bin — stream firmware binary"""
        import os
        firmware_path = os.path.join(self.OTA_DIR, "tinkertab.bin")
        if not os.path.exists(firmware_path):
            return web.Response(text="No firmware available", status=404)

        file_size = os.path.getsize(firmware_path)
        resp = web.StreamResponse()
        resp.content_type = "application/octet-stream"
        resp.content_length = file_size
        resp.headers["Content-Disposition"] = "attachment; filename=tinkertab.bin"
        await resp.prepare(request)

        with open(firmware_path, "rb") as f:
            while chunk := f.read(8192):
                await resp.write(chunk)
        return resp
