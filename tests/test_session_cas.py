"""
Audit C2 regression test: update_session_status_if is supposed to be
an atomic compare-and-swap so two concurrent disconnects on the same
session can't both fire "session.paused" events. The audit flagged
"no concurrency test — grep for update_session_status_if finds only
its definition + one caller".

This test spawns N concurrent pause-transition coroutines and asserts
exactly one wins (returns True), the rest lose (return False) — i.e.
atomic CAS behaviour.
"""

import asyncio
import time
import secrets
import tempfile
import pathlib

import pytest

from dragon_voice.db import Database


@pytest.mark.asyncio
async def test_update_session_status_if_is_atomic(tmp_path: pathlib.Path) -> None:
    """Spawn 10 concurrent CAS calls; exactly 1 should win."""
    db_path = tmp_path / "cas.db"
    db = Database(str(db_path))
    await db.initialize()

    device_id = secrets.token_hex(6)
    session_id = secrets.token_hex(8)
    now = time.time()
    # Device first (FK dependency for sessions.device_id)
    await db.conn.execute(
        """INSERT INTO devices (id, hardware_id, created_at, updated_at)
           VALUES (?, ?, ?, ?)""",
        (device_id, f"hw-{device_id}", now, now),
    )
    await db.conn.execute(
        """INSERT INTO sessions (id, device_id, created_at, last_active_at,
           status, message_count)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (session_id, device_id, now, now, "active", 0),
    )
    await db.conn.commit()

    # Fire 10 parallel "active → paused" transitions. If CAS is atomic,
    # exactly one gets True, the others False.
    async def transition() -> bool:
        return await db.update_session_status_if(
            session_id, "paused", "active"
        )

    results = await asyncio.gather(*[transition() for _ in range(10)])
    winners = [r for r in results if r]
    losers = [r for r in results if not r]

    assert len(winners) == 1, (
        f"CAS not atomic: {len(winners)} coroutines won the transition "
        f"(expected exactly 1). Results: {results}"
    )
    assert len(losers) == 9

    # Row should be in 'paused' state.
    cursor = await db.conn.execute(
        "SELECT status FROM sessions WHERE id = ?", (session_id,)
    )
    row = await cursor.fetchone()
    assert row["status"] == "paused"

    await db.close()


@pytest.mark.asyncio
async def test_cas_rejects_wrong_expected(tmp_path: pathlib.Path) -> None:
    """If expected_current doesn't match actual, UPDATE skips."""
    db_path = tmp_path / "cas2.db"
    db = Database(str(db_path))
    await db.initialize()

    device_id = secrets.token_hex(6)
    session_id = secrets.token_hex(8)
    now = time.time()
    # Device first (FK dependency for sessions.device_id)
    await db.conn.execute(
        """INSERT INTO devices (id, hardware_id, created_at, updated_at)
           VALUES (?, ?, ?, ?)""",
        (device_id, f"hw-{device_id}", now, now),
    )
    await db.conn.execute(
        """INSERT INTO sessions (id, device_id, created_at, last_active_at,
           status, message_count)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (session_id, device_id, now, now, "active", 0),
    )
    await db.conn.commit()

    # CAS expecting 'ended' when actual is 'active' → False, no change.
    ok = await db.update_session_status_if(session_id, "paused", "ended")
    assert ok is False

    cursor = await db.conn.execute(
        "SELECT status FROM sessions WHERE id = ?", (session_id,)
    )
    row = await cursor.fetchone()
    assert row["status"] == "active", "Status should be unchanged on CAS miss"

    await db.close()
