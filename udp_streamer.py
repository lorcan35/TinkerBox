#!/usr/bin/env python3
"""
UDP JPEG Streamer — KMS/DRM framebuffer grab + UDP chunked JPEG sender

Captures the Dragon's screen using the fastest available method:
  1. ffmpeg kmsgrab (HW-accelerated on RK3588, ~2-5ms)
  2. grim (Wayland compositor screenshot)
  3. Xlib + Pillow (X11 fallback)
  4. CDP Page.captureScreenshot (last resort, 80-200ms)

Encodes each frame as JPEG and sends via UDP to the Tab5 (ESP32-P4).

Chunking protocol (fits within MTU without IP fragmentation):
  [frame_num:4B BE][chunk_idx:2B BE][total_chunks:2B BE][jpeg_data]
  chunk_size = 1400 bytes payload per packet

Target: 15 fps with adaptive frame dropping.
"""

import asyncio
import io
import logging
import math
import os
import shutil
import signal
import socket
import struct
import subprocess
import time
from typing import Optional, Tuple

log = logging.getLogger("udp_streamer")

# -------------------------------------------------------------------
# Config defaults
# -------------------------------------------------------------------
DEFAULT_TARGET_IP = None        # Must be set via argument or mDNS
DEFAULT_UDP_PORT = 5000
DEFAULT_JPEG_QUALITY = 50
DEFAULT_FPS = 15
DEFAULT_WIDTH = 720
DEFAULT_HEIGHT = 1280
CHUNK_PAYLOAD = 1400            # bytes of JPEG data per UDP packet
MDNS_SERVICE = "_tinkertab._udp.local."

# -------------------------------------------------------------------
# Capture backends
# -------------------------------------------------------------------

class CaptureBackend:
    """Base class for screen capture backends."""
    name: str = "base"
    async def capture(self) -> Optional[bytes]:
        """Return raw JPEG bytes of the current screen, or None on failure."""
        raise NotImplementedError
    async def start(self):
        pass
    async def stop(self):
        pass


class FFmpegKMSGrab(CaptureBackend):
    """
    Uses ffmpeg with kmsgrab input — reads the KMS/DRM framebuffer directly.
    Fastest path on RK3588 (~2-5ms). Requires CAP_SYS_ADMIN or root, and
    DRM device access (/dev/dri/card*).
    """
    name = "ffmpeg-kmsgrab"

    def __init__(self, width: int, height: int, quality: int):
        self.width = width
        self.height = height
        self.quality = quality
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._drm_device: Optional[str] = None

    def _find_drm_device(self) -> Optional[str]:
        """Find a usable DRM device (prefer renderD*, then card*)."""
        import glob
        for pattern in ["/dev/dri/card*"]:
            devs = sorted(glob.glob(pattern))
            for d in devs:
                if os.access(d, os.R_OK):
                    return d
        return None

    async def start(self):
        self._drm_device = self._find_drm_device()
        if not self._drm_device:
            raise RuntimeError("No accessible DRM device found")
        log.info("ffmpeg-kmsgrab: using DRM device %s", self._drm_device)

    async def capture(self) -> Optional[bytes]:
        """Grab a single frame via ffmpeg kmsgrab -> JPEG pipe."""
        cmd = [
            "ffmpeg", "-y",
            "-device", self._drm_device,
            "-f", "kmsgrab",
            "-framerate", "1",          # single frame
            "-i", "-",
            "-vf", f"hwmap=derive_device=vaapi,hwdownload,format=bgr0,scale={self.width}:{self.height}",
            "-frames:v", "1",
            "-q:v", str(max(1, min(31, 31 - int(self.quality * 31 / 100)))),
            "-f", "mjpeg",
            "pipe:1",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            if proc.returncode == 0 and stdout:
                return bytes(stdout)
            return None
        except (asyncio.TimeoutError, Exception) as exc:
            log.debug("kmsgrab capture failed: %s", exc)
            return None

    async def stop(self):
        pass


class FFmpegX11Grab(CaptureBackend):
    """
    Uses ffmpeg with x11grab — captures X11 display via shared memory.
    Much faster than CDP screenshots (~5-15ms).
    """
    name = "ffmpeg-x11grab"

    def __init__(self, width: int, height: int, quality: int):
        self.width = width
        self.height = height
        self.quality = quality
        self._display: Optional[str] = None

    async def start(self):
        self._display = os.environ.get("DISPLAY", ":0")
        log.info("ffmpeg-x11grab: using DISPLAY=%s", self._display)

    async def capture(self) -> Optional[bytes]:
        # ffmpeg quality: -q:v ranges 2 (best) to 31 (worst)
        qv = max(2, min(31, 31 - int(self.quality * 29 / 100)))
        cmd = [
            "ffmpeg", "-y",
            "-f", "x11grab",
            "-video_size", f"{self.width}x{self.height}",
            "-framerate", "30",
            "-i", self._display,
            "-frames:v", "1",
            "-q:v", str(qv),
            "-f", "mjpeg",
            "pipe:1",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            if proc.returncode == 0 and stdout:
                return bytes(stdout)
            return None
        except (asyncio.TimeoutError, Exception) as exc:
            log.debug("x11grab capture failed: %s", exc)
            return None

    async def stop(self):
        pass


class GrimCapture(CaptureBackend):
    """Uses grim for Wayland compositors (sway, etc.)."""
    name = "grim"

    def __init__(self, width: int, height: int, quality: int):
        self.width = width
        self.height = height
        self.quality = quality

    async def capture(self) -> Optional[bytes]:
        cmd = [
            "grim",
            "-t", "jpeg",
            "-q", str(self.quality),
            "-s", str(self.width / 1920),  # scale factor (approximate)
            "-",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            if proc.returncode == 0 and stdout:
                return bytes(stdout)
            return None
        except (asyncio.TimeoutError, Exception):
            return None

    async def stop(self):
        pass


class PillowXlibCapture(CaptureBackend):
    """Pillow ImageGrab via Xlib — pure Python X11 screenshot."""
    name = "pillow-xlib"

    def __init__(self, width: int, height: int, quality: int):
        self.width = width
        self.height = height
        self.quality = quality

    async def capture(self) -> Optional[bytes]:
        try:
            from PIL import ImageGrab
            loop = asyncio.get_event_loop()
            img = await loop.run_in_executor(None, self._grab)
            return img
        except Exception as exc:
            log.debug("pillow-xlib capture failed: %s", exc)
            return None

    def _grab(self) -> Optional[bytes]:
        from PIL import ImageGrab
        img = ImageGrab.grab()
        img = img.resize((self.width, self.height))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=self.quality)
        return buf.getvalue()

    async def stop(self):
        pass


class CDPCapture(CaptureBackend):
    """
    Falls back to Chrome DevTools Protocol Page.captureScreenshot.
    Slow (80-200ms) but always available when Chromium is running.
    """
    name = "cdp-screenshot"

    def __init__(self, width: int, height: int, quality: int,
                 cdp_host: str = "127.0.0.1", cdp_port: int = 9222):
        self.width = width
        self.height = height
        self.quality = quality
        self.cdp_host = cdp_host
        self.cdp_port = cdp_port
        self._ws = None
        self._session = None
        self._msg_id = 1

    async def start(self):
        import aiohttp
        self._session = aiohttp.ClientSession()
        try:
            async with self._session.get(
                f"http://{self.cdp_host}:{self.cdp_port}/json"
            ) as resp:
                targets = await resp.json()
            ws_url = None
            for t in targets:
                if t.get("type") == "page":
                    ws_url = t["webSocketDebuggerUrl"]
                    break
            if ws_url:
                self._ws = await self._session.ws_connect(
                    ws_url, max_msg_size=10 * 1024 * 1024
                )
                log.info("cdp-screenshot: connected to %s", ws_url)
        except Exception as exc:
            log.warning("cdp-screenshot: failed to connect: %s", exc)

    async def capture(self) -> Optional[bytes]:
        if not self._ws or self._ws.closed:
            return None
        import base64, aiohttp
        await self._ws.send_json({
            "id": self._msg_id,
            "method": "Page.captureScreenshot",
            "params": {
                "format": "jpeg",
                "quality": self.quality,
                "clip": {
                    "x": 0, "y": 0,
                    "width": self.width, "height": self.height,
                    "scale": 1,
                },
            },
        })
        self._msg_id += 1
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    import json
                    data = json.loads(msg.data)
                    if "result" in data and "data" in data.get("result", {}):
                        return base64.b64decode(data["result"]["data"])
                    if "error" in data:
                        return None
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    return None
        except Exception:
            return None
        return None

    async def stop(self):
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session:
            await self._session.close()


# -------------------------------------------------------------------
# Auto-detect best capture backend
# -------------------------------------------------------------------

async def detect_backend(width: int, height: int, quality: int) -> CaptureBackend:
    """Probe available capture methods and return the best one."""

    has_ffmpeg = shutil.which("ffmpeg") is not None
    has_grim = shutil.which("grim") is not None
    wayland = os.environ.get("WAYLAND_DISPLAY") is not None
    x11 = os.environ.get("DISPLAY") is not None

    # 1. Try ffmpeg kmsgrab (needs DRM access)
    if has_ffmpeg:
        try:
            backend = FFmpegKMSGrab(width, height, quality)
            await backend.start()
            frame = await backend.capture()
            if frame:
                log.info("Selected backend: ffmpeg-kmsgrab (fastest)")
                return backend
        except Exception as exc:
            log.debug("kmsgrab probe failed: %s", exc)

    # 2. Try ffmpeg x11grab (X11)
    if has_ffmpeg and x11:
        try:
            backend = FFmpegX11Grab(width, height, quality)
            await backend.start()
            frame = await backend.capture()
            if frame:
                log.info("Selected backend: ffmpeg-x11grab")
                return backend
        except Exception as exc:
            log.debug("x11grab probe failed: %s", exc)

    # 3. Try grim (Wayland)
    if has_grim and wayland:
        try:
            backend = GrimCapture(width, height, quality)
            frame = await backend.capture()
            if frame:
                log.info("Selected backend: grim (Wayland)")
                return backend
        except Exception:
            pass

    # 4. Try Pillow + Xlib
    if x11:
        try:
            backend = PillowXlibCapture(width, height, quality)
            frame = await backend.capture()
            if frame:
                log.info("Selected backend: pillow-xlib")
                return backend
        except Exception:
            pass

    # 5. CDP fallback
    log.info("Falling back to CDP screenshot capture")
    backend = CDPCapture(width, height, quality)
    await backend.start()
    return backend


# -------------------------------------------------------------------
# mDNS Tab5 discovery
# -------------------------------------------------------------------

async def discover_tab5_mdns(timeout: float = 5.0) -> Optional[str]:
    """Try to discover Tab5 IP via mDNS (zeroconf)."""
    try:
        from zeroconf import Zeroconf, ServiceBrowser
    except ImportError:
        log.debug("zeroconf not installed — mDNS discovery unavailable")
        return None

    found_ip: Optional[str] = None
    event = asyncio.Event()

    class Listener:
        def add_service(self, zc, stype, name):
            nonlocal found_ip
            info = zc.get_service_info(stype, name)
            if info and info.addresses:
                found_ip = socket.inet_ntoa(info.addresses[0])
                log.info("mDNS: discovered Tab5 at %s", found_ip)
                event.set()

        def remove_service(self, *_):
            pass

        def update_service(self, *_):
            pass

    loop = asyncio.get_event_loop()
    zc = Zeroconf()
    browser = ServiceBrowser(zc, MDNS_SERVICE, Listener())

    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        log.info("mDNS: no Tab5 found within %.1fs", timeout)
    finally:
        zc.close()

    return found_ip


# -------------------------------------------------------------------
# UDP chunked sender
# -------------------------------------------------------------------

def send_frame_udp(sock: socket.socket, dest: Tuple[str, int],
                   jpeg_data: bytes, frame_num: int):
    """
    Send one JPEG frame over UDP using the chunking protocol.

    Packet layout:
      [frame_num : 4 bytes big-endian uint32]
      [chunk_idx : 2 bytes big-endian uint16]
      [total_chunks : 2 bytes big-endian uint16]
      [jpeg_data_chunk : up to CHUNK_PAYLOAD bytes]
    """
    total = math.ceil(len(jpeg_data) / CHUNK_PAYLOAD) if jpeg_data else 1
    for i in range(total):
        offset = i * CHUNK_PAYLOAD
        chunk = jpeg_data[offset : offset + CHUNK_PAYLOAD]
        header = struct.pack("!IHH", frame_num & 0xFFFFFFFF, i, total)
        sock.sendto(header + chunk, dest)


# -------------------------------------------------------------------
# Main streamer loop
# -------------------------------------------------------------------

class UDPStreamer:
    """
    Async screen-capture + UDP JPEG streamer.

    Usage:
        streamer = UDPStreamer(target_ip="192.168.1.100")
        await streamer.start()    # auto-detects capture backend, opens socket
        ...
        await streamer.stop()
    """

    def __init__(
        self,
        target_ip: Optional[str] = None,
        target_port: int = DEFAULT_UDP_PORT,
        jpeg_quality: int = DEFAULT_JPEG_QUALITY,
        fps: int = DEFAULT_FPS,
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
    ):
        self.target_ip = target_ip
        self.target_port = target_port
        self.jpeg_quality = jpeg_quality
        self.fps = fps
        self.width = width
        self.height = height

        self._backend: Optional[CaptureBackend] = None
        self._sock: Optional[socket.socket] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False

        # Stats
        self.frames_sent = 0
        self.frames_dropped = 0
        self.current_fps = 0.0
        self.backend_name = "none"
        self.last_capture_ms = 0.0

    async def start(self):
        """Detect capture backend, resolve target IP, begin streaming."""
        # Resolve target IP
        if not self.target_ip:
            log.info("No target IP provided — trying mDNS discovery...")
            self.target_ip = await discover_tab5_mdns()
        if not self.target_ip:
            log.warning("No target IP and mDNS failed. UDP streamer will wait "
                        "for set_target() call.")

        # Detect capture backend
        self._backend = await detect_backend(self.width, self.height, self.jpeg_quality)
        self.backend_name = self._backend.name
        log.info("Capture backend: %s", self.backend_name)

        # Open UDP socket
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setblocking(False)

        # Start capture loop
        self._running = True
        self._task = asyncio.create_task(self._stream_loop())
        log.info("UDP streamer started → %s:%d @ %d fps (q=%d)",
                 self.target_ip or "<pending>", self.target_port,
                 self.fps, self.jpeg_quality)

    def set_target(self, ip: str, port: int = DEFAULT_UDP_PORT):
        """Update target IP/port at runtime (e.g., after Tab5 handshake)."""
        self.target_ip = ip
        self.target_port = port
        log.info("UDP target updated: %s:%d", ip, port)

    async def _stream_loop(self):
        """Main capture-encode-send loop with adaptive frame dropping."""
        frame_interval = 1.0 / self.fps
        frame_num = 0
        fps_counter = 0
        fps_timer = time.monotonic()

        while self._running:
            loop_start = time.monotonic()

            if not self.target_ip:
                # No target yet — wait and retry
                await asyncio.sleep(0.5)
                continue

            dest = (self.target_ip, self.target_port)

            try:
                # Capture
                t0 = time.monotonic()
                jpeg = await self._backend.capture()
                capture_ms = (time.monotonic() - t0) * 1000
                self.last_capture_ms = capture_ms

                if jpeg:
                    # Send via UDP
                    send_frame_udp(self._sock, dest, jpeg, frame_num)
                    frame_num += 1
                    self.frames_sent += 1
                    fps_counter += 1
                else:
                    self.frames_dropped += 1

            except Exception as exc:
                log.error("Stream loop error: %s", exc)
                self.frames_dropped += 1

            # FPS stats (every 5 seconds)
            now = time.monotonic()
            elapsed_stats = now - fps_timer
            if elapsed_stats >= 5.0:
                self.current_fps = fps_counter / elapsed_stats
                log.info("[UDP] %.1f fps, capture: %.1fms, sent: %d, dropped: %d, backend: %s",
                         self.current_fps, self.last_capture_ms,
                         self.frames_sent, self.frames_dropped, self.backend_name)
                fps_counter = 0
                fps_timer = now

            # Adaptive sleep — skip frame if we're behind schedule
            elapsed = time.monotonic() - loop_start
            sleep_time = frame_interval - elapsed
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
            else:
                # We're behind — yield to event loop but don't wait
                self.frames_dropped += 1
                await asyncio.sleep(0)

    async def stop(self):
        """Stop the streamer gracefully."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._backend:
            await self._backend.stop()
        if self._sock:
            self._sock.close()
        log.info("UDP streamer stopped. Sent: %d frames, Dropped: %d",
                 self.frames_sent, self.frames_dropped)

    def stats(self) -> dict:
        """Return current streamer statistics."""
        return {
            "backend": self.backend_name,
            "target": f"{self.target_ip}:{self.target_port}" if self.target_ip else None,
            "fps": round(self.current_fps, 1),
            "frames_sent": self.frames_sent,
            "frames_dropped": self.frames_dropped,
            "capture_ms": round(self.last_capture_ms, 1),
            "jpeg_quality": self.jpeg_quality,
            "resolution": f"{self.width}x{self.height}",
        }


# -------------------------------------------------------------------
# CLI entry point (standalone usage)
# -------------------------------------------------------------------

async def main():
    import argparse
    parser = argparse.ArgumentParser(description="UDP JPEG Streamer for TinkerClaw")
    parser.add_argument("--target", "-t", help="Tab5 IP address")
    parser.add_argument("--port", "-p", type=int, default=DEFAULT_UDP_PORT,
                        help=f"UDP port (default: {DEFAULT_UDP_PORT})")
    parser.add_argument("--quality", "-q", type=int, default=DEFAULT_JPEG_QUALITY,
                        help=f"JPEG quality 1-100 (default: {DEFAULT_JPEG_QUALITY})")
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS,
                        help=f"Target FPS (default: {DEFAULT_FPS})")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH,
                        help=f"Capture width (default: {DEFAULT_WIDTH})")
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT,
                        help=f"Capture height (default: {DEFAULT_HEIGHT})")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    streamer = UDPStreamer(
        target_ip=args.target,
        target_port=args.port,
        jpeg_quality=args.quality,
        fps=args.fps,
        width=args.width,
        height=args.height,
    )

    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def _signal_handler():
        log.info("Signal received, shutting down...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    await streamer.start()

    await stop_event.wait()
    await streamer.stop()


if __name__ == "__main__":
    asyncio.run(main())
