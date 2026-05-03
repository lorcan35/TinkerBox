"""SQLite corruption-recovery helper for `Database.initialize`.

Wave 23 SOLID-audit follow-up — thirty-third sub-extract.
First slice from `dragon_voice/db.py` (audit SRP-7 (P2): the
844-LOC `Database` class is a monolith of 30+ method domains).

When `Database.initialize` opens the SQLite file and detects
corruption (PRAGMA integrity_check fails OR aiosqlite raises
on connect), this module's `recover_corrupt_db` runs the
4-step recovery chain:

  1. Close any half-open connection.
  2. Rename the corrupt -wal and -shm sidecar files (preserve
     for diagnosis with `.corrupt.<timestamp>` suffix).
  3. Try opening the main DB file alone — it should be
     consistent up to the last successful WAL checkpoint, so
     after removing the corrupt WAL the main file is often
     fine and we can preserve user data.
  4. If the main DB is also corrupt, rename the entire DB file
     and let `Database.initialize` recreate from `schema.sql`.

Pre-extract this 70-LOC chain lived as `Database._recover_corrupt_db`.
Now lives in its own module, taking the db path + the
already-opened (failed) connection as args.

## API

```python
db_recreated = await recover_corrupt_db(db_path, current_connection)
# `db_recreated` is True iff step 4 fired (main DB renamed →
# fresh DB needed).  False means step 3 succeeded (main DB
# intact, just WAL was bad).
```

The caller is responsible for re-opening the connection +
re-running schema migrations after this returns.

## Why a free function (not a class)

Recovery is a one-shot operation; no state needs to persist
across calls.  The `db_path` + the dead connection are passed
explicitly so the recovery is testable in isolation (no
Database instance needed).

## Step 3 vs. Step 4 distinction

Step 3 success is the common case (corrupt WAL is far more
common than corrupt main file — power loss during a write
typically only damages the WAL).  When it fires, the user
loses at most the un-checkpointed transactions since the last
WAL flush — usually < 1 minute of state.

Step 4 is the catastrophic path: full DB rebuild.  The
corrupt file is preserved for manual recovery, but the user
sees an empty DB on next boot.  Logged at ERROR for ops
visibility.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)


async def recover_corrupt_db(
    db_path: str,
    current_connection: Optional[aiosqlite.Connection],
) -> bool:
    """Attempt to recover from a corrupt SQLite database.

    Args:
        db_path: Filesystem path to the SQLite DB file.
        current_connection: The already-opened (failed)
            aiosqlite connection, or None if open didn't get
            that far.  Will be closed (best-effort).

    Returns:
        True iff the main DB file was unsalvageable (step 4
        fired — main file renamed, caller should let
        `Database.initialize` recreate from schema).
        False iff step 3 succeeded (corrupt WAL/SHM removed,
        main DB intact, caller can re-open normally).
    """
    # Step 1: Close existing connection (best-effort).
    if current_connection is not None:
        try:
            await current_connection.close()
        except Exception:
            pass

    db_path_obj = Path(db_path)
    ts = int(time.time())

    # Step 2: Rename corrupt WAL + SHM sidecars.
    _rename_corrupt_sidecars(db_path_obj, ts)

    # Step 3: Try opening the main DB without the WAL.
    # The main file is consistent up to the last successful
    # WAL checkpoint — after removing the corrupt WAL, this
    # often succeeds and preserves all but the un-checkpointed
    # transactions.
    test_db: Optional[aiosqlite.Connection] = None
    try:
        test_db = await aiosqlite.connect(db_path)
        await test_db.execute("PRAGMA integrity_check")
        await test_db.close()
        logger.info(
            "Main database file is intact after removing corrupt WAL/SHM — "
            "data up to last checkpoint is preserved",
        )
        return False  # Main DB OK, caller re-opens normally
    except Exception as retry_exc:
        logger.error(
            "Main database also corrupt after WAL removal: %s — "
            "creating fresh database",
            retry_exc,
        )
        if test_db is not None:
            try:
                await test_db.close()
            except Exception:
                pass

    # Step 4: Rename the entire DB file.  Caller will recreate
    # via aiosqlite.connect + schema.sql.
    _rename_corrupt_main_db(db_path_obj, ts)

    logger.error(
        "Recovery complete — a fresh empty database will be created. "
        "Corrupt files preserved with .corrupt.%d suffix for diagnosis.",
        ts,
    )
    return True


def _rename_corrupt_sidecars(db_path: Path, ts: int) -> None:
    """Rename the -wal and -shm sidecars to `.corrupt.<ts>`.
    Preserves both for ops to recover from manually if needed."""
    wal_path = db_path.with_name(db_path.name + "-wal")
    shm_path = db_path.with_name(db_path.name + "-shm")

    for sidecar in (wal_path, shm_path):
        if sidecar.exists():
            backup = sidecar.with_name(f"{sidecar.name}.corrupt.{ts}")
            sidecar.rename(backup)
            logger.error(
                "Renamed corrupt sidecar: %s -> %s",
                sidecar, backup.name,
            )


def _rename_corrupt_main_db(db_path: Path, ts: int) -> None:
    """Rename the main DB file to `.corrupt.<ts>` for ops
    diagnosis.  No-op when the file doesn't exist (the very
    first boot has no DB yet)."""
    if db_path.exists():
        backup = db_path.with_name(f"{db_path.name}.corrupt.{ts}")
        db_path.rename(backup)
        logger.error(
            "Renamed corrupt database: %s -> %s  "
            "(recover data manually from this backup)",
            db_path, backup.name,
        )
