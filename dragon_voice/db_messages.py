"""Message CRUD — free async functions taking an aiosqlite connection.

Wave 23 SOLID-audit follow-up — thirty-sixth sub-extract.
Fourth slice from `dragon_voice/db.py` (audit SRP-7).

Pre-extract these were 6 methods on `Database` (~155 LOC of
SQL).  Now lives as free functions taking the connection
explicitly.

## API

```python
msg = await add_message(conn, message_id="m1", session_id="s1",
                        role="user", content="hello")
msgs = await get_messages(conn, "s1", limit=100, offset=0)
n = await count_messages(conn, "s1")
m = await get_message(conn, "m1")
counts = await purge_old_messages(conn, days=30, batch_size=500)
n_deleted = await delete_messages(conn, "s1")
```

## Wave 14 W14-H10 batched purge invariant preserved

The purge runs in `batch_size`-row chunks with
`await asyncio.sleep(0)` between batches.  Pre-fix a single
`DELETE ... WHERE NOT IN (SELECT ...)` ran on the shared
aiosqlite connection — on a large tail it serialized every
other coroutine behind 2-3 s of purge.  Now WS keepalives and
receipt emits can progress even on a huge purge.

## DQ04 PASSIVE checkpoint preserved

After bulk deletes, a `PRAGMA wal_checkpoint(PASSIVE)` moves
WAL pages back into the main DB file without blocking
readers.  Reduces WAL file growth and consolidates writes to
reduce eMMC wear.

## add_message side-effect

Calls into the sessions module's `increment_message_count`
to maintain the denormalized counter on the session row.
The order is: insert message → commit → increment counter
(separate commit).  Pre-extract this depended on
`self.increment_message_count`; now we import the
`db_sessions` helper to maintain the same side effect.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)


# Default batch size for the purge loop.  500 rows × ~few-ms
# per batch keeps each yield window short enough that WS
# keepalives + receipt emits don't starve.
_PURGE_BATCH_SIZE = 500


async def add_message(
    conn: aiosqlite.Connection,
    *,
    message_id: str,
    session_id: str,
    role: str,
    content: str,
    input_mode: str = "text",
    interrupted: bool = False,
    audio_duration_s: Optional[float] = None,
    token_count: Optional[int] = None,
    model: Optional[str] = None,
    latency_ms: Optional[float] = None,
) -> dict:
    """Insert an append-only message.  Returns the message row
    as dict.

    Side effect: increments the denormalized
    `sessions.message_count` for the parent session.
    """
    now = time.time()
    await conn.execute(
        """
        INSERT INTO messages (id, session_id, role, content, input_mode, interrupted,
                              audio_duration_s, token_count, model, latency_ms, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id, session_id, role, content, input_mode,
            1 if interrupted else 0, audio_duration_s, token_count,
            model, latency_ms, now,
        ),
    )
    await conn.commit()

    # Update denormalized count on the session row.
    from dragon_voice.db_sessions import increment_message_count
    await increment_message_count(conn, session_id)

    cursor = await conn.execute(
        "SELECT * FROM messages WHERE id = ?", (message_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else {}


async def get_messages(
    conn: aiosqlite.Connection,
    session_id: str,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Get messages for a session, ordered by creation time
    (ascending — chat-replay order)."""
    cursor = await conn.execute(
        """
        SELECT * FROM messages WHERE session_id = ?
        ORDER BY created_at ASC LIMIT ? OFFSET ?
        """,
        (session_id, limit, offset),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def count_messages(
    conn: aiosqlite.Connection,
    session_id: str,
) -> int:
    """Count messages in a session."""
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ?",
        (session_id,),
    )
    row = await cursor.fetchone()
    return row[0] if row else 0


async def get_message(
    conn: aiosqlite.Connection,
    message_id: str,
) -> Optional[dict]:
    """Fetch a single message by ID.  Returns None when not
    found."""
    cursor = await conn.execute(
        "SELECT * FROM messages WHERE id = ?", (message_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def purge_old_messages(
    conn: aiosqlite.Connection,
    *,
    days: int = 30,
    batch_size: int = _PURGE_BATCH_SIZE,
) -> dict[str, int]:
    """Purge messages and orphaned events older than `days`.

    Skips messages belonging to active or paused sessions to
    avoid deleting context from sessions still in use.

    Wave 14 W14-H10: the delete runs in `batch_size`-row chunks
    with `await asyncio.sleep(0)` between batches.  Pre-fix a
    single DELETE ... WHERE NOT IN (SELECT ...) ran on the
    shared aiosqlite connection — on a large tail it
    serialized every other coroutine behind 2-3 s of purge.

    Returns dict with counts: {"messages": N, "events": M}.
    days <= 0 disables purge (returns zeros).
    """
    if days <= 0:
        logger.info("Message purge disabled (days=%d)", days)
        return {"messages": 0, "events": 0}

    cutoff = time.time() - (days * 86400)

    msg_count = await _batched_delete_messages(conn, cutoff, batch_size)
    evt_count = await _batched_delete_events(conn, cutoff, batch_size)

    # DQ04 PASSIVE checkpoint after bulk deletes — moves WAL
    # pages back into the main DB file without blocking readers.
    # Reduces WAL file growth and consolidates writes to reduce
    # eMMC wear.
    if msg_count > 0 or evt_count > 0:
        try:
            await conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            logger.info("WAL passive checkpoint after purge")
        except Exception as ckpt_err:
            logger.warning(
                "WAL checkpoint after purge failed: %s", ckpt_err,
            )

    logger.info(
        "Purged %d messages and %d events older than %d days",
        msg_count, evt_count, days,
    )
    return {"messages": msg_count, "events": evt_count}


async def _batched_delete_messages(
    conn: aiosqlite.Connection,
    cutoff: float,
    batch_size: int,
) -> int:
    """Delete messages older than `cutoff` in batches; skip
    messages whose session is still active/paused.  Yields the
    event loop between batches so other coroutines can progress.
    """
    total = 0
    while True:
        cursor = await conn.execute(
            """
            DELETE FROM messages
            WHERE rowid IN (
                SELECT rowid FROM messages
                WHERE created_at < ?
                  AND session_id NOT IN (
                      SELECT id FROM sessions WHERE status IN ('active', 'paused')
                  )
                LIMIT ?
            )
            """,
            (cutoff, batch_size),
        )
        deleted = cursor.rowcount
        await conn.commit()
        total += deleted
        if deleted < batch_size:
            break
        await asyncio.sleep(0)  # yield the loop between batches
    return total


async def _batched_delete_events(
    conn: aiosqlite.Connection,
    cutoff: float,
    batch_size: int,
) -> int:
    """Delete events older than `cutoff` in batches; skip events
    whose session is still active/paused (and orphan events with
    NULL session_id are eligible)."""
    total = 0
    while True:
        cursor = await conn.execute(
            """
            DELETE FROM events
            WHERE rowid IN (
                SELECT rowid FROM events
                WHERE created_at < ?
                  AND (session_id IS NULL
                       OR session_id NOT IN (
                           SELECT id FROM sessions WHERE status IN ('active', 'paused')
                       ))
                LIMIT ?
            )
            """,
            (cutoff, batch_size),
        )
        deleted = cursor.rowcount
        await conn.commit()
        total += deleted
        if deleted < batch_size:
            break
        await asyncio.sleep(0)
    return total


async def delete_messages(
    conn: aiosqlite.Connection,
    session_id: str,
) -> int:
    """Delete all messages for a session.  Returns count
    deleted.  Resets the denormalized `sessions.message_count`
    to 0."""
    count = await count_messages(conn, session_id)
    await conn.execute(
        "DELETE FROM messages WHERE session_id = ?", (session_id,),
    )
    # Reset denormalized count
    await conn.execute(
        "UPDATE sessions SET message_count = 0 WHERE id = ?",
        (session_id,),
    )
    await conn.commit()
    return count
