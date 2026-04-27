"""Tests for dragon_voice.api.video_inject.

Just the framing helper — the route handler is exercised end-to-end
in the live deploy smoke (no live aiohttp test rig in the named CI set
yet for upload-style endpoints).
"""

from __future__ import annotations

import struct

from dragon_voice.api.video_inject import (
    VIDEO_MAGIC,
    VIDEO_MAX_PAYLOAD,
    _wrap_video_frame,
)


def test_wrap_prepends_magic_and_be_length():
    payload = b"\xff\xd8\xff\xd9"
    out = _wrap_video_frame(payload)
    assert out[:4] == VIDEO_MAGIC
    (length,) = struct.unpack(">I", out[4:8])
    assert length == len(payload)
    assert out[8:] == payload


def test_wrap_round_trips_through_parser():
    """Should produce bytes the Tab5 / Dragon parser accepts."""
    from dragon_voice.video_upstream import parse_video_frame
    jpeg = b"\xff\xd8\xff\xd9" + b"x" * 1000
    wire = _wrap_video_frame(jpeg)
    body = parse_video_frame(wire)
    assert body == jpeg


def test_payload_ceiling_is_sane():
    assert VIDEO_MAX_PAYLOAD == 96 * 1024
