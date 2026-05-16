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
            # #338: pre-TTS cleaner — strips markdown / bullets / code
            # fences before synthesis so REST /synthesize callers get
            # the same spoken-flow benefits the WS-voice path gets.
            if getattr(self._config.tts, "text_cleaner_enabled", True):
                from dragon_voice.tts import clean_for_tts
                text = clean_for_tts(text) or text
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
        import asyncio as _asyncio
        import os
        import re as _re
        current = request.query.get("current", "0.0.0")

        # Wave 14 W14-H08: offload the sync file stat + open to a thread
        # so the event loop doesn't stall even if eMMC is slow.  The
        # file is tiny (<1 KB) so the to_thread overhead is negligible.
        def _load_version_file():
            if not os.path.exists(self.OTA_VERSION_FILE):
                return None
            try:
                with open(self.OTA_VERSION_FILE) as f:
                    return json.load(f)
            except Exception:
                return None
        info = await _asyncio.to_thread(_load_version_file)
        if info is None:
            return web.json_response({"update": False, "current": current})

        available_ver = info.get("version", "0.0.0")
        sha256 = info.get("sha256", "")

        # Wave 10 fix: parse version as a tuple of ints so 0.10.0 beats 0.8.0
        # (the old `available <= current` string compare rejected 0.10.x
        # because '1' < '8' lexicographically). Any non-numeric suffix
        # (e.g. "-wave10") is stripped before parsing.
        def _parts(v: str) -> tuple:
            head = _re.split(r"[-+]", str(v).lstrip("v"))[0]
            bits = []
            for piece in head.split("."):
                m = _re.match(r"\d+", piece)
                bits.append(int(m.group(0)) if m else 0)
            return tuple(bits)

        try:
            avail_parts = _parts(available_ver)
            cur_parts = _parts(current)
        except Exception:
            avail_parts = cur_parts = ()

        if avail_parts <= cur_parts:
            return web.json_response({"update": False, "current": current, "available": available_ver})

        # Wave 14 W14-L05: prefer the canonical URL baked into
        # version.json over request.host.  A misconfigured reverse-
        # proxy or an unexpected Host header could otherwise trick
        # Tab5 into downloading firmware from the wrong place.
        # The SHA256 check in ota.c is the real integrity gate, but
        # this tightens the layer above.  Fall back to request.host
        # for back-compat with version.json files that don't include
        # a url.
        firmware_url = info.get("url")
        if not firmware_url:
            host = request.host
            scheme = request.scheme
            firmware_url = f"{scheme}://{host}/api/ota/firmware.bin"
        return web.json_response({
            "update": True, "version": available_ver,
            "url": firmware_url, "sha256": sha256,
        })

    async def ota_firmware(self, request: web.Request) -> web.StreamResponse:
        """GET /api/ota/firmware.bin — stream firmware binary.

        Wave 14 W14-H08: prior code stalled the event loop on every
        8 KB `f.read()` chunk (eMMC page cycles ~15-100 ms on Radxa).
        ``web.FileResponse`` hands the file to sendfile(2) on Linux —
        kernel-level copy, zero event-loop blocking, and lower latency
        for the firmware download Tab5 makes on every Settings tap.
        """
        import os
        firmware_path = os.path.join(self.OTA_DIR, "tinkertab.bin")
        if not os.path.exists(firmware_path):
            return web.Response(text="No firmware available", status=404)
        return web.FileResponse(
            path=firmware_path,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Disposition": "attachment; filename=tinkertab.bin",
            },
        )
