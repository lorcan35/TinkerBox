"""Tests for dragon_voice.video_upstream — the Tab5 → Dragon video frame parser."""

from __future__ import annotations

import asyncio
import os
import struct
import tempfile
from pathlib import Path

import pytest

from dragon_voice.video_upstream import (
    VIDEO_HEADER_LEN,
    VIDEO_MAGIC,
    VideoUpstreamHandler,
    parse_video_frame,
)


def _wrap(jpeg: bytes) -> bytes:
    return VIDEO_MAGIC + struct.pack(">I", len(jpeg)) + jpeg


def test_peek_recognises_magic():
    assert parse_video_frame.peek(_wrap(b"\xff\xd8\xff\xd9"))


def test_peek_rejects_short_audio_bytes():
    assert not parse_video_frame.peek(b"\x00")
    assert not parse_video_frame.peek(b"\x12\x34\x56\x78\x9a")  # raw PCM-ish


def test_peek_rejects_empty():
    assert not parse_video_frame.peek(b"")


def test_parse_extracts_jpeg_payload():
    jpeg = b"\xff\xd8\xff\xd9" + b"hello" * 10
    body = parse_video_frame(_wrap(jpeg))
    assert body == jpeg


def test_parse_rejects_bad_magic():
    bad = b"BAD!" + struct.pack(">I", 4) + b"\xff\xd8\xff\xd9"
    with pytest.raises(ValueError, match="bad magic"):
        parse_video_frame(bad)


def test_parse_rejects_short_frame():
    with pytest.raises(ValueError, match="too short"):
        parse_video_frame(b"VID")  # 3 bytes


def test_parse_rejects_len_mismatch():
    bad = VIDEO_MAGIC + struct.pack(">I", 100) + b"only10byte"
    with pytest.raises(ValueError, match="len mismatch"):
        parse_video_frame(bad)


def test_handler_writes_latest_frame_to_disk(tmp_path):
    latest = tmp_path / "latest.jpg"
    h = VideoUpstreamHandler(latest_path=str(latest))
    jpeg = b"\xff\xd8\xff\xd9" + b"x" * 64
    asyncio.run(h.on_frame("sess1", "dev1", _wrap(jpeg)))
    assert latest.exists()
    assert latest.read_bytes() == jpeg
    s = h.stats("sess1")
    assert s["frames"] == 1
    assert s["last_jpeg_bytes"] == len(jpeg)
    assert s["parse_errors"] == 0


def test_handler_counts_parse_errors_quietly(tmp_path):
    latest = tmp_path / "latest.jpg"
    h = VideoUpstreamHandler(latest_path=str(latest))
    asyncio.run(h.on_frame("s", "d", b"BAD!" + b"\x00" * 4 + b"junk"))
    s = h.stats("s")
    assert s["frames"] == 0
    assert s["parse_errors"] == 1
    assert not latest.exists()
