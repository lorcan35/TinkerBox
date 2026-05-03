"""Tests for ``dragon_voice.db_corruption_recovery``.

Pin the four-step recovery chain end-to-end:
  1. Close existing connection (best-effort, swallow errors)
  2. Rename corrupt -wal and -shm sidecars
  3. Try opening the main DB alone — return False on success
     (caller re-opens normally; user data preserved)
  4. Main DB also corrupt → rename it, return True (caller
     recreates from schema)
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dragon_voice.db_corruption_recovery import (
    _rename_corrupt_main_db,
    _rename_corrupt_sidecars,
    recover_corrupt_db,
)


@pytest.fixture
def tmp_db_path():
    """Create a real on-disk SQLite file path for the test, with
    sidecars to exercise the rename logic."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        # Touch the file so step 4's `if db_path.exists()` fires.
        db.touch()
        yield str(db)


# ─── Sidecar rename helper ──────────────────────────────────


class TestSidecarRename:
    def test_renames_existing_wal_and_shm(self, tmp_db_path):
        db_path = Path(tmp_db_path)
        wal = db_path.with_name(db_path.name + "-wal")
        shm = db_path.with_name(db_path.name + "-shm")
        wal.write_text("corrupt-wal")
        shm.write_text("corrupt-shm")

        _rename_corrupt_sidecars(db_path, ts=12345)

        # Originals gone, backups present
        assert not wal.exists()
        assert not shm.exists()
        assert db_path.with_name(f"{wal.name}.corrupt.12345").exists()
        assert db_path.with_name(f"{shm.name}.corrupt.12345").exists()

    def test_no_sidecars_is_silent_noop(self, tmp_db_path):
        """When no sidecars exist (clean shutdown, fresh boot),
        the rename helper must not crash."""
        db_path = Path(tmp_db_path)
        # No sidecars — must not raise.
        _rename_corrupt_sidecars(db_path, ts=12345)


# ─── Main DB rename helper ──────────────────────────────────


class TestMainDbRename:
    def test_renames_existing_db_with_timestamp_suffix(self, tmp_db_path):
        db_path = Path(tmp_db_path)
        db_path.write_text("corrupt-content")

        _rename_corrupt_main_db(db_path, ts=12345)

        # Original gone, backup present
        assert not db_path.exists()
        backup = db_path.with_name(f"{db_path.name}.corrupt.12345")
        assert backup.exists()
        assert backup.read_text() == "corrupt-content"

    def test_missing_db_is_silent_noop(self):
        """First-boot case: no DB file yet.  The helper must
        not crash trying to rename a non-existent path."""
        # Path that doesn't exist
        # Must not raise.
        _rename_corrupt_main_db(
            Path("/nonexistent/path/db.sqlite"), ts=12345,
        )


# ─── Full recovery chain — step 3 success path ───────────────


class TestRecoveryStep3Success:
    @pytest.mark.asyncio
    async def test_returns_false_when_main_db_intact(self, tmp_db_path):
        """When the WAL was the only corrupt thing, removing it
        + opening the main DB succeeds → return False (caller
        re-opens normally; user data preserved)."""
        # Create realistic sidecars
        db_path_obj = Path(tmp_db_path)
        wal = db_path_obj.with_name(db_path_obj.name + "-wal")
        wal.write_text("corrupt")

        # Mock aiosqlite.connect to return a successful test_db
        test_db_mock = MagicMock()
        test_db_mock.execute = AsyncMock()
        test_db_mock.close = AsyncMock()

        with patch(
            "aiosqlite.connect",
            new=AsyncMock(return_value=test_db_mock),
        ):
            result = await recover_corrupt_db(tmp_db_path, None)

        assert result is False
        # Sidecar was renamed
        assert not wal.exists()
        # Main DB file still present (step 4 didn't fire)
        assert db_path_obj.exists()


# ─── Full recovery chain — step 4 fires (main also corrupt) ─


class TestRecoveryStep4Fires:
    @pytest.mark.asyncio
    async def test_returns_true_when_main_db_also_corrupt(self, tmp_db_path):
        """When the main DB integrity_check ALSO fails, rename
        the entire DB → return True (caller creates fresh)."""
        db_path_obj = Path(tmp_db_path)

        # First call (step 3): aiosqlite raises → main DB
        # detected as corrupt
        with patch(
            "aiosqlite.connect",
            new=AsyncMock(side_effect=RuntimeError("file is not a database")),
        ):
            result = await recover_corrupt_db(tmp_db_path, None)

        assert result is True
        # Main DB renamed
        assert not db_path_obj.exists()
        # Backup present (timestamp-suffixed)
        backups = list(db_path_obj.parent.glob(f"{db_path_obj.name}.corrupt.*"))
        assert len(backups) == 1


# ─── Connection-close best-effort ────────────────────────────


class TestConnectionCloseBestEffort:
    @pytest.mark.asyncio
    async def test_closes_provided_connection(self, tmp_db_path):
        conn = MagicMock()
        conn.close = AsyncMock()

        # Mock recovery step 3 so we don't open a real DB
        test_db = MagicMock()
        test_db.execute = AsyncMock()
        test_db.close = AsyncMock()

        with patch(
            "aiosqlite.connect", new=AsyncMock(return_value=test_db),
        ):
            await recover_corrupt_db(tmp_db_path, conn)

        conn.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_failure_swallowed(self, tmp_db_path):
        """If the dead connection's close() raises (already
        broken), the recovery chain must still proceed."""
        conn = MagicMock()
        conn.close = AsyncMock(side_effect=RuntimeError("conn dead"))

        test_db = MagicMock()
        test_db.execute = AsyncMock()
        test_db.close = AsyncMock()

        with patch(
            "aiosqlite.connect", new=AsyncMock(return_value=test_db),
        ):
            # Must NOT raise.
            result = await recover_corrupt_db(tmp_db_path, conn)

        assert result is False  # step 3 succeeded

    @pytest.mark.asyncio
    async def test_no_connection_provided_does_not_crash(self, tmp_db_path):
        """When `current_connection=None` (open didn't get that
        far), skip the close step silently."""
        test_db = MagicMock()
        test_db.execute = AsyncMock()
        test_db.close = AsyncMock()

        with patch(
            "aiosqlite.connect", new=AsyncMock(return_value=test_db),
        ):
            await recover_corrupt_db(tmp_db_path, None)

        # Just must not raise


# ─── Timestamp suffix consistency ───────────────────────────


class TestTimestampSuffix:
    def test_sidecar_and_main_use_same_timestamp(self, tmp_db_path):
        """When step 4 fires after step 2 already renamed
        sidecars, both renames should use the SAME timestamp so
        ops can correlate the backup files."""
        db_path = Path(tmp_db_path)
        wal = db_path.with_name(db_path.name + "-wal")
        wal.write_text("x")

        # Pre-existing main DB content
        db_path.write_text("main")

        # Pin the timestamp so both helpers use the same value
        with patch(
            "dragon_voice.db_corruption_recovery.time.time",
            return_value=99999,
        ), patch(
            "aiosqlite.connect",
            new=AsyncMock(side_effect=RuntimeError("corrupt")),
        ):
            import asyncio
            asyncio.run(recover_corrupt_db(tmp_db_path, None))

        # Both backups present with same timestamp
        wal_backup = db_path.with_name(f"{wal.name}.corrupt.99999")
        db_backup = db_path.with_name(f"{db_path.name}.corrupt.99999")
        assert wal_backup.exists()
        assert db_backup.exists()
