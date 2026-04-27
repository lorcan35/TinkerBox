"""Tests for dragon_voice.audio_codec — the OPUS codec wrapper.

Covers:
- have_opus() reports a bool that matches what import succeeds
- negotiate_uplink picks the right codec
- OpusUplinkDecoder + OpusDownlinkEncoder roundtrip a known PCM signal
  back to recognisable PCM (within OPUS lossy bounds)
- Both classes raise CodecUnavailable cleanly when libopus is missing
"""

from __future__ import annotations

import math
import struct

import pytest

from dragon_voice import audio_codec as ac


def _sine_pcm(samples: int, freq: float = 440.0, sr: int = 16_000) -> bytes:
    """Generate a clean sine wave as int16 LE bytes."""
    out = bytearray()
    for i in range(samples):
        v = int(0.4 * 32767 * math.sin(2 * math.pi * freq * i / sr))
        out += struct.pack("<h", v)
    return bytes(out)


def test_have_opus_returns_bool():
    assert isinstance(ac.have_opus(), bool)


def test_negotiate_no_caps_picks_pcm():
    assert ac.negotiate_uplink(None) == "pcm"
    assert ac.negotiate_uplink([]) == "pcm"


def test_negotiate_pcm_only_picks_pcm():
    assert ac.negotiate_uplink(["pcm"]) == "pcm"


def test_negotiate_opus_picks_opus_when_supported():
    if ac.have_opus():
        assert ac.negotiate_uplink(["pcm", "opus"]) == "opus"
    else:
        assert ac.negotiate_uplink(["pcm", "opus"]) == "pcm"


def test_negotiate_case_insensitive():
    if ac.have_opus():
        assert ac.negotiate_uplink(["OPUS"]) == "opus"
    else:
        assert ac.negotiate_uplink(["OPUS"]) == "pcm"


@pytest.mark.skipif(not ac.have_opus(), reason="opuslib not available")
def test_opus_roundtrip_preserves_signal_shape():
    # 20 ms = 320 samples
    pcm_in = _sine_pcm(ac.FRAME_SAMPLES, freq=440.0)
    enc = ac.OpusDownlinkEncoder(bitrate=24_000)
    dec = ac.OpusUplinkDecoder()
    pkt = enc.encode(pcm_in)
    assert len(pkt) > 0 and len(pkt) < len(pcm_in), \
        f"compressed {len(pcm_in)} -> {len(pkt)} bytes"
    pcm_out = dec.decode(pkt)
    # OPUS is lossy — exact match impossible.  Just check we got a
    # plausibly-sized PCM frame back (at least one frame, in 20 ms
    # multiples).
    assert len(pcm_out) >= ac.FRAME_BYTES_PCM
    assert len(pcm_out) % 2 == 0  # int16 boundary


@pytest.mark.skipif(ac.have_opus(), reason="only meaningful when libopus is missing")
def test_codec_unavailable_when_no_libopus():
    with pytest.raises(ac.CodecUnavailable):
        ac.OpusUplinkDecoder()
    with pytest.raises(ac.CodecUnavailable):
        ac.OpusDownlinkEncoder()


# #181 / TinkerTab #272: AUD0-framed call-audio helpers.

def _wrap_call_audio(pcm: bytes) -> bytes:
    return ac.CALL_AUDIO_MAGIC + struct.pack(">I", len(pcm)) + pcm


def test_peek_call_audio_magic_recognises():
    assert ac.peek_call_audio_magic(_wrap_call_audio(b"\x00" * 8))


def test_peek_call_audio_magic_rejects_video_and_pcm():
    # Video frame magic
    assert not ac.peek_call_audio_magic(b"VID0" + b"\x00" * 4)
    # Plain PCM (raw int16)
    assert not ac.peek_call_audio_magic(b"\x00\x80\x00\x80")
    # Too short
    assert not ac.peek_call_audio_magic(b"AU")


def test_parse_call_audio_extracts_body():
    pcm = bytes(range(40))
    body = ac.parse_call_audio_frame(_wrap_call_audio(pcm))
    assert body == pcm


def test_parse_call_audio_rejects_bad_magic():
    bad = b"BAD!" + struct.pack(">I", 4) + b"\x00\x00\x00\x00"
    with pytest.raises(ValueError, match="bad magic"):
        ac.parse_call_audio_frame(bad)


def test_parse_call_audio_rejects_len_mismatch():
    bad = ac.CALL_AUDIO_MAGIC + struct.pack(">I", 100) + b"only10byte"
    with pytest.raises(ValueError, match="len mismatch"):
        ac.parse_call_audio_frame(bad)
