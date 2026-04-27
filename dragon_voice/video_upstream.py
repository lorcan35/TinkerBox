"""Video upstream from Tab5 (#175 / TinkerTab #266) + relay (#179 Phase 3C).

Accepts JPEG frames sent by Tab5 over the existing voice WebSocket as
binary frames prefixed with the 4-byte magic tag ``b"VID0"`` + a
big-endian uint32 length.

Two consumers per inbound frame:
- Disk snapshot (most-recent frame at /home/radxa/media/...) for
  inspection + the dashboard preview.
- **Relay broadcast**: forwards the wire bytes verbatim to every
  other connected client so two Tab5s (or a Tab5 + a web client) see
  each other's video in real time.  This is the simplest possible
  pairing model — anyone who's connected and isn't the sender gets
  the frame.

Caller pattern (server.py binary handler):

    from .video_upstream import parse_video_frame, get_handler

    if parse_video_frame.peek(msg.data):
        await get_handler().on_frame(
            session_id, device_id, msg.data,
            active_connections=server._active_connections,
        )
    else:
        await pipeline.feed_audio(msg.data)
"""

from __future__ import annotations

import logging
import os
import struct
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

VIDEO_MAGIC = b"VID0"
VIDEO_HEADER_LEN = 8         # 4 magic + 4 length
# /tmp would be private under systemd's PrivateTmp=true, so dashboards
# + scp users can't see the frame.  Use the existing media dir instead.
DEFAULT_LATEST_PATH = "/home/radxa/media/tab5_video_latest.jpg"


@dataclass
class _SessionVideoStats:
    frames: int = 0
    bytes_in: int = 0
    last_jpeg_bytes: int = 0
    last_ts: float = 0.0
    parse_errors: int = 0


class VideoUpstreamHandler:
    """Per-server singleton that processes inbound video frames.

    Currently only persists the most recent frame to disk so a human
    can spot-check the pipeline.  Future phases plug a relay queue +
    optional vision-LLM dispatch in here.
    """

    def __init__(self, latest_path: str = DEFAULT_LATEST_PATH) -> None:
        self._latest_path = latest_path
        self._stats: dict[str, _SessionVideoStats] = {}

    async def on_frame(
        self,
        session_id: str,
        device_id: str,
        wire_bytes: bytes,
        active_connections: dict | None = None,
    ) -> None:
        """Validate + extract + persist + relay.

        `active_connections` is the server-wide ws_id → conn_state
        registry; when provided, every other connected client receives
        the same wire bytes verbatim.  Quietly drops malformed frames
        so noisy clients don't spam the journal.
        """
        s = self._stats.setdefault(session_id, _SessionVideoStats())
        try:
            jpeg = parse_video_frame(wire_bytes)
        except ValueError as e:
            s.parse_errors += 1
            logger.debug("video parse error sess=%s dev=%s: %s",
                         session_id, device_id, e)
            return

        s.frames += 1
        s.bytes_in += len(wire_bytes)
        s.last_jpeg_bytes = len(jpeg)
        s.last_ts = time.time()

        # Most-recent-frame snapshot.  Atomic-ish write so a reader
        # never sees a half-written file.
        tmp = self._latest_path + ".part"
        try:
            with open(tmp, "wb") as f:
                f.write(jpeg)
            os.replace(tmp, self._latest_path)
        except OSError as e:
            logger.warning("video latest-write failed: %s", e)

        # Relay broadcast: forward the wire bytes to every other
        # connected client.  Phase 3C minimum-viable pairing; later
        # work will add explicit call signaling so unpaired peers
        # don't see each other's feeds.
        relayed = 0
        if active_connections:
            for conn in list(active_connections.values()):
                if conn.get("session_id") == session_id:
                    continue
                ws = conn.get("ws")
                if ws is None or getattr(ws, "closed", False):
                    continue
                try:
                    await ws.send_bytes(wire_bytes)
                    relayed += 1
                except Exception as e:
                    logger.debug("video relay drop: %s", e)

        if s.frames == 1 or s.frames % 20 == 0:
            logger.info(
                "video frame #%d from %s session=%s (%d B JPEG, %d B wire) relayed=%d",
                s.frames, device_id, session_id,
                len(jpeg), len(wire_bytes), relayed,
            )

    def stats(self, session_id: str) -> dict:
        s = self._stats.get(session_id)
        if not s:
            return {"frames": 0, "bytes_in": 0, "last_jpeg_bytes": 0,
                    "last_ts": 0.0, "parse_errors": 0}
        return {
            "frames":          s.frames,
            "bytes_in":        s.bytes_in,
            "last_jpeg_bytes": s.last_jpeg_bytes,
            "last_ts":         s.last_ts,
            "parse_errors":    s.parse_errors,
        }


_handler: VideoUpstreamHandler | None = None


def get_handler() -> VideoUpstreamHandler:
    global _handler
    if _handler is None:
        _handler = VideoUpstreamHandler()
    return _handler


def parse_video_frame(wire_bytes: bytes) -> bytes:
    """Parse one wire-format frame; return the JPEG payload bytes.

    Raises ValueError on any malformed prefix.
    """
    if len(wire_bytes) < VIDEO_HEADER_LEN:
        raise ValueError(f"too short: {len(wire_bytes)} B")
    if wire_bytes[:4] != VIDEO_MAGIC:
        raise ValueError("bad magic")
    (length,) = struct.unpack(">I", wire_bytes[4:8])
    body = wire_bytes[VIDEO_HEADER_LEN:]
    if len(body) != length:
        raise ValueError(f"len mismatch: header={length} body={len(body)}")
    return body


def _peek(wire_bytes: bytes) -> bool:
    """True iff this looks like a video frame (4-byte magic match)."""
    return len(wire_bytes) >= 4 and wire_bytes[:4] == VIDEO_MAGIC


# Expose .peek as a bound attribute so callers can write
#   parse_video_frame.peek(data)
# instead of importing two names.
parse_video_frame.peek = _peek  # type: ignore[attr-defined]
