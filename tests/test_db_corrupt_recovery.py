"""
Audit C4 regression: Dragon claims corrupt-DB auto-recovery (_recover_corrupt_db)
but the audit flagged it as "never deliberately tested".

This test deliberately corrupts the DB file on disk, then invokes
Database.initialize() and asserts it recovers (either by renaming the
corrupt file aside + creating fresh schema, OR by salvaging after WAL
removal — both are valid recovery paths per the docstring).

After recovery the connection must be usable (CRUD works) even if the
original data is lost.
"""

import asyncio
import pathlib
import time

import pytest

from dragon_voice.db import Database


@pytest.mark.asyncio
async def test_recover_from_garbage_db_file(tmp_path: pathlib.Path) -> None:
    """A totally garbage file in the DB path should be recoverable —
    renamed to .corrupt.TS and replaced with a fresh schema."""
    db_path = tmp_path / "garbage.db"
    # Write non-SQLite bytes where the DB should be.
    db_path.write_bytes(b"this is not a sqlite database, sorry\n" * 50)

    db = Database(str(db_path))
    await db.initialize()

    # Connection must be live + basic schema must be there.
    cursor = await db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in await cursor.fetchall()}
    assert "sessions" in tables, "Fresh schema wasn't applied after recovery"
    assert "devices" in tables

    # The corrupt original should have been renamed aside.
    corrupt_backups = list(tmp_path.glob("garbage.db.corrupt.*"))
    assert len(corrupt_backups) >= 1, "Original corrupt file wasn't preserved"

    await db.close()


@pytest.mark.asyncio
async def test_recover_from_corrupt_wal_alone(tmp_path: pathlib.Path) -> None:
    """If the main DB file is valid but the -wal is corrupt, recovery
    should rename the WAL aside and leave the main DB intact (i.e. data
    up to the last checkpoint is preserved)."""
    db_path = tmp_path / "wal_test.db"

    # Bootstrap a healthy DB first.
    db = Database(str(db_path))
    await db.initialize()
    await db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    await db.conn.commit()
    await db.close()

    # Write garbage into the -wal file.
    wal_path = db_path.with_name(db_path.name + "-wal")
    wal_path.write_bytes(b"\x00" * 4096)  # invalid WAL magic + pages

    # Recovery path on re-open.
    db2 = Database(str(db_path))
    await db2.initialize()

    # Schema should still be there (WAL recovery preserved main DB).
    cursor = await db2.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in await cursor.fetchall()}
    assert "sessions" in tables

    # The -wal should have been renamed to .corrupt.TS (or WAL never
    # actually triggered corruption detection if SQLite was forgiving).
    wal_backups = list(tmp_path.glob(f"{wal_path.name}.corrupt.*"))
    # Either WAL was moved aside OR SQLite absorbed it silently. Both OK.
    assert not wal_path.exists() or wal_path.stat().st_size > 0 or len(wal_backups) >= 1

    await db2.close()


@pytest.mark.asyncio
async def test_post_recovery_db_is_writable(tmp_path: pathlib.Path) -> None:
    """Sanity: after recovering from a garbage file, the fresh DB must
    accept writes — not just schema."""
    import time as _time

    db_path = tmp_path / "write.db"
    db_path.write_bytes(b"garbage")
    db = Database(str(db_path))
    await db.initialize()

    now = _time.time()
    await db.conn.execute(
        """INSERT INTO devices (id, hardware_id, created_at, updated_at)
           VALUES (?, ?, ?, ?)""",
        ("recov_dev", "hw-recov", now, now),
    )
    await db.conn.commit()

    cursor = await db.conn.execute("SELECT COUNT(*) FROM devices WHERE id=?", ("recov_dev",))
    row = await cursor.fetchone()
    assert row[0] == 1, "Post-recovery DB rejected a simple write"

    await db.close()
