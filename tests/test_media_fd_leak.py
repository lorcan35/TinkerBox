"""Wave 15 W15-C03 regression: PIL `Image.open` must not leak FDs.

Before the fix, every call to `_resize_jpeg` (the hot-path JPEG resizer
used for code-block renders, table renders, upload re-encodes) leaked
one file descriptor because `Image.open` returns a lazy handle and the
code never called `.close()` or used a context manager.  Under the
hour-long memory-monitor window this was slow but measurable — over
1 000 renders the process could hit the 1024 FD limit.

This test renders 50 images through `_resize_jpeg`, checks the FD
count from `/proc/self/fd` before and after, and fails if growth is
more than the OS noise floor.
"""

from __future__ import annotations

import io
import os

from PIL import Image

from dragon_voice.media.pipeline import _resize_jpeg


def _fd_count() -> int:
    return len(os.listdir(f"/proc/{os.getpid()}/fd"))


def _make_png(w: int = 128, h: int = 128) -> bytes:
    """Build a tiny PNG payload so we don't need any fixtures on disk."""
    img = Image.new("RGB", (w, h), color=(200, 50, 100))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    img.close()
    return buf.getvalue()


def test_resize_jpeg_does_not_leak_fds():
    payload = _make_png()

    # Warm up once so any lazy-import / module-load FDs are already
    # accounted for in the baseline.
    _resize_jpeg(payload, max_width=64)

    before = _fd_count()
    for _ in range(50):
        out = _resize_jpeg(payload, max_width=64)
        assert isinstance(out, bytes) and len(out) > 0
    after = _fd_count()

    # The OS allocator and pytest can wobble by a few FDs.  We just
    # need to prove there's no O(N)-per-call leak — 50 calls with a
    # leaky code path would show +50, the fixed code path stays tiny.
    assert after - before < 10, (
        f"FD growth {after - before} across 50 _resize_jpeg calls — "
        "PIL Image.open context-manager regression?"
    )


def test_resize_jpeg_preserves_bytes_output():
    payload = _make_png(256, 128)
    out = _resize_jpeg(payload, max_width=128)
    # Should be a smaller JPEG than the source PNG.
    assert isinstance(out, bytes)
    with Image.open(io.BytesIO(out)) as result:
        assert result.size == (128, 64)  # aspect preserved, shrunk
        assert result.format == "JPEG"


def test_resize_jpeg_noop_when_already_small():
    payload = _make_png(32, 32)
    out = _resize_jpeg(payload, max_width=200)
    with Image.open(io.BytesIO(out)) as result:
        assert result.size == (32, 32)
