"""Unit tests for dragon_voice.media.store.MediaStore.

Tests cover:
- store() saves bytes and returns a valid media_id
- get_path() returns the correct path for a stored file
- get_path() returns None for an unknown ID
- get_path() sanitises path-traversal attempts
- cleanup() deletes files older than max_age_hours
- cleanup() keeps recent files
- cleanup() enforces max_total_mb by removing oldest files first
- cleanup() is a no-op when the media directory does not exist
"""

import asyncio
import os
import time
import tempfile
from pathlib import Path

import pytest

from dragon_voice.media.store import MediaStore


# ── Helpers ─────────────────────────────────────────────────────────────────


def make_store(tmp_path: Path, max_age_hours: float = 24, max_total_mb: float = 500) -> MediaStore:
    return MediaStore(
        media_dir=str(tmp_path / "media"),
        max_age_hours=max_age_hours,
        max_total_mb=max_total_mb,
    )


# ── Store tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_returns_media_id(tmp_path):
    store = make_store(tmp_path)
    media_id = await store.store(b"hello world", "txt")
    assert isinstance(media_id, str)
    assert media_id.endswith(".txt")
    # ID part is exactly 12 hex chars + dot + ext
    name_part = media_id.rsplit(".", 1)[0]
    assert len(name_part) == 12
    assert all(c in "0123456789abcdef" for c in name_part)


@pytest.mark.asyncio
async def test_store_writes_bytes_to_disk(tmp_path):
    store = make_store(tmp_path)
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    media_id = await store.store(payload, "png", session_id="sess-abc")

    path = await store.get_path(media_id)
    assert path is not None
    assert Path(path).read_bytes() == payload


@pytest.mark.asyncio
async def test_store_creates_media_dir_if_missing(tmp_path):
    media_dir = tmp_path / "deeply" / "nested" / "media"
    store = MediaStore(media_dir=str(media_dir))
    assert not media_dir.exists()
    await store.store(b"data", "bin")
    assert media_dir.exists()


@pytest.mark.asyncio
async def test_store_ids_are_unique(tmp_path):
    store = make_store(tmp_path)
    ids = [await store.store(b"x", "jpg") for _ in range(50)]
    assert len(set(ids)) == 50


# ── get_path tests ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_path_returns_none_for_unknown_id(tmp_path):
    store = make_store(tmp_path)
    # Create the dir so we're definitely checking absence, not missing dir
    await store.store(b"seed", "bin")
    result = await store.get_path("nonexistent.png")
    assert result is None


@pytest.mark.asyncio
async def test_get_path_sanitises_path_traversal(tmp_path):
    store = make_store(tmp_path)
    # Ensure media dir exists
    await store.store(b"seed", "bin")

    evil_ids = [
        "../../etc/passwd",
        "../secrets.txt",
        "/etc/passwd",
        "subdir/sneaky.png",
    ]
    for evil in evil_ids:
        result = await store.get_path(evil)
        assert result is None, f"Expected None for {evil!r}, got {result!r}"


@pytest.mark.asyncio
async def test_get_path_empty_string_returns_none(tmp_path):
    store = make_store(tmp_path)
    assert await store.get_path("") is None


# ── cleanup tests ────────────────────────────────────────────────────────────


def _set_mtime(path: Path, seconds_ago: float) -> None:
    """Backdate a file's mtime."""
    ts = time.time() - seconds_ago
    os.utime(str(path), (ts, ts))


@pytest.mark.asyncio
async def test_cleanup_removes_old_files(tmp_path):
    store = make_store(tmp_path, max_age_hours=1)
    media_id = await store.store(b"old data", "txt")

    path = Path(await store.get_path(media_id))
    _set_mtime(path, seconds_ago=7200)  # 2 hours old → beyond 1h limit

    await store.cleanup()

    assert not path.exists()
    assert await store.get_path(media_id) is None


@pytest.mark.asyncio
async def test_cleanup_keeps_recent_files(tmp_path):
    store = make_store(tmp_path, max_age_hours=1)
    media_id = await store.store(b"fresh data", "txt")

    path = Path(await store.get_path(media_id))
    _set_mtime(path, seconds_ago=60)  # 1 minute old → well within 1h limit

    await store.cleanup()

    assert path.exists()
    assert await store.get_path(media_id) is not None


@pytest.mark.asyncio
async def test_cleanup_enforces_size_cap(tmp_path):
    # Cap at 1 MB; store 3 files of ~400 KB each = ~1.2 MB total
    store = make_store(tmp_path, max_age_hours=9999, max_total_mb=1.0)

    chunk = b"A" * (400 * 1024)  # 400 KB

    id1 = await store.store(chunk, "bin")
    id2 = await store.store(chunk, "bin")
    id3 = await store.store(chunk, "bin")

    # Backdate id1 and id2 so they are older than id3
    p1 = Path(await store.get_path(id1))
    p2 = Path(await store.get_path(id2))
    p3 = Path(await store.get_path(id3))
    _set_mtime(p1, seconds_ago=300)
    _set_mtime(p2, seconds_ago=200)
    _set_mtime(p3, seconds_ago=100)

    await store.cleanup()

    # Only the newest file (id3) should survive; id1 and id2 trimmed to fit 1 MB
    assert p3.exists(), "Newest file should survive size cleanup"
    assert not p1.exists(), "Oldest file should be removed by size cap"


@pytest.mark.asyncio
async def test_cleanup_noop_when_dir_missing(tmp_path):
    """cleanup() must not raise when the media directory doesn't exist yet."""
    store = make_store(tmp_path, max_age_hours=1)
    media_dir = tmp_path / "media"
    assert not media_dir.exists()
    # Should complete without raising
    await store.cleanup()


@pytest.mark.asyncio
async def test_cleanup_does_not_remove_files_within_budget(tmp_path):
    """Files within age + size budget must all survive."""
    store = make_store(tmp_path, max_age_hours=24, max_total_mb=500)
    ids = []
    for _ in range(5):
        ids.append(await store.store(b"small", "bin"))

    await store.cleanup()

    for mid in ids:
        assert await store.get_path(mid) is not None, f"{mid} was wrongly removed"
