"""Audio codec wrapper for the Tab5 ↔ Dragon voice WS (#173 / TinkerTab #262).

Wraps `opuslib` for the specific shape of our pipeline: 16 kHz mono,
16-bit PCM, 20 ms frames (320 samples = 640 B per chunk).

Backward-compat: if `opuslib` is not importable (system missing libopus),
the module still imports cleanly but `decode_uplink` / `encode_downlink`
raise CodecUnavailable.  Callers should treat that as "OPUS not
supported, stay on PCM".

Negotiation lives in server.py + the WS register/config_update path;
this module just does the per-frame conversion.
"""

from __future__ import annotations

import logging
import struct
from typing import Optional

logger = logging.getLogger(__name__)

# Wire-format constants — must match Tab5 voice.c / voice_codec.c.
SAMPLE_RATE = 16_000
CHANNELS = 1
FRAME_MS = 20
FRAME_SAMPLES = (SAMPLE_RATE // 1000) * FRAME_MS  # 320
FRAME_BYTES_PCM = FRAME_SAMPLES * 2               # 640


class CodecUnavailable(RuntimeError):
    """Raised when the requested codec can't be initialised on this host."""


try:
    import opuslib  # type: ignore[import-untyped]
    _HAVE_OPUSLIB = True
except Exception as e:  # pragma: no cover - depends on system libopus
    opuslib = None  # type: ignore[assignment]
    _HAVE_OPUSLIB = False
    _OPUSLIB_IMPORT_ERROR = e


def have_opus() -> bool:
    """True iff libopus + the python wrapper are available on this host."""
    return _HAVE_OPUSLIB


class OpusUplinkDecoder:
    """Mic-uplink decoder.  Stateful — one instance per WS connection.

    `decode(packet)` takes one OPUS packet (the bytes of one binary WS
    frame from Tab5) and returns 16-bit PCM bytes (typically 640 B for
    a 20 ms packet, but variable based on the encoder's frame size).
    """

    def __init__(self) -> None:
        if not _HAVE_OPUSLIB:
            raise CodecUnavailable(
                f"opuslib not available: {_OPUSLIB_IMPORT_ERROR}"
            )
        self._dec = opuslib.Decoder(SAMPLE_RATE, CHANNELS)

    def decode(self, packet: bytes) -> bytes:
        if not packet:
            return b""
        # opuslib.Decoder.decode(data, frame_size) — frame_size is the
        # max number of samples per channel the decoded frame might
        # produce.  120 ms @ 16 kHz = 1920 is the absolute max OPUS
        # supports; we use that to be safe.
        try:
            pcm = self._dec.decode(packet, 1920, decode_fec=False)
        except Exception as e:
            logger.warning("OPUS decode failed (len=%d): %s", len(packet), e)
            return b""
        return pcm


class OpusDownlinkEncoder:
    """TTS-downlink encoder (Phase 2B — used by the TTS send path).

    `encode(pcm)` takes one 20 ms PCM frame (640 B) and returns the
    OPUS-encoded packet.  Caller is responsible for chunking the TTS
    stream into 20 ms frames.
    """

    def __init__(self, bitrate: int = 24_000) -> None:
        if not _HAVE_OPUSLIB:
            raise CodecUnavailable(
                f"opuslib not available: {_OPUSLIB_IMPORT_ERROR}"
            )
        # OPUS application: VOIP — best for voice intelligibility at
        # low-to-moderate bitrates.  Matches Tab5's encoder choice.
        self._enc = opuslib.Encoder(SAMPLE_RATE, CHANNELS, opuslib.APPLICATION_VOIP)
        try:
            self._enc.bitrate = bitrate
        except Exception:
            # Some opuslib builds expose bitrate via .ctl(); fall back
            # silently rather than failing — the default ~32 kbps is
            # acceptable.
            pass

    def encode(self, pcm: bytes) -> bytes:
        if len(pcm) % FRAME_BYTES_PCM != 0:
            raise ValueError(
                f"encode: pcm len {len(pcm)} not a multiple of {FRAME_BYTES_PCM}"
            )
        out = bytearray()
        for off in range(0, len(pcm), FRAME_BYTES_PCM):
            frame = pcm[off:off + FRAME_BYTES_PCM]
            try:
                pkt = self._enc.encode(frame, FRAME_SAMPLES)
            except Exception as e:
                logger.warning("OPUS encode failed: %s", e)
                continue
            out += pkt
        return bytes(out)


# #181 / TinkerTab #272: in-call audio framing.  When Tab5 is in a
# video call (voice_video_is_in_call() == true), mic frames are
# wrapped with this 4-byte magic + 4-byte BE length so Dragon
# broadcasts them to peers instead of feeding STT.  Symmetric on the
# downlink — peers' tagged frames play through Tab5's existing
# playback ring buffer.  Wire body is raw int16 LE PCM @ 16 kHz mono
# (or whatever the mic uplink codec produces).
CALL_AUDIO_MAGIC = b"AUD0"
CALL_AUDIO_HEADER_LEN = 8


def peek_call_audio_magic(data: bytes) -> bool:
    """True iff `data` starts with the AUD0 magic."""
    return len(data) >= 4 and data[:4] == CALL_AUDIO_MAGIC


def parse_call_audio_frame(wire_bytes: bytes) -> bytes:
    """Parse one AUD0-framed audio chunk; return the body (PCM) bytes.

    Raises ValueError on malformed input — server.py logs at debug
    level so noisy clients don't spam the journal.
    """
    if len(wire_bytes) < CALL_AUDIO_HEADER_LEN:
        raise ValueError(f"too short: {len(wire_bytes)} B")
    if wire_bytes[:4] != CALL_AUDIO_MAGIC:
        raise ValueError("bad magic")
    (length,) = struct.unpack(">I", wire_bytes[4:8])
    body = wire_bytes[CALL_AUDIO_HEADER_LEN:]
    if len(body) != length:
        raise ValueError(f"len mismatch: header={length} body={len(body)}")
    return body


def negotiate_uplink(client_caps: Optional[list[str]]) -> str:
    """Pick a codec given the client's advertised list.

    Returns one of "pcm" / "opus".  Falls back to "pcm" if the client
    didn't advertise (legacy) or if OPUS isn't available locally.
    """
    if not client_caps:
        return "pcm"
    wants_opus = any(c.lower() == "opus" for c in client_caps)
    if wants_opus and have_opus():
        return "opus"
    return "pcm"
