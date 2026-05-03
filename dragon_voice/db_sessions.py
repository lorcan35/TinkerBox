"""Session CRUD — free async functions taking an aiosqlite connection.

Wave 23 SOLID-audit follow-up — thirty-fifth sub-extract.
Third slice from `dragon_voice/db.py` (audit SRP-7).

Pre-extract these were 9 methods on `Database` (~150 LOC of
SQL).  Now lives as free functions taking the connection
explicitly.

## API

```python
session = await create_session(conn, session_id="s1", device_id="d1")
session = await get_session(conn, "s1")
sessions = await list_sessions(conn, device_id="d1", status="active")
await update_session_status(conn, "s1", "paused")
swapped = await update_session_status_if(conn, "s1", "ended", "active")
await touch_session(conn, "s1")
await increment_message_count(conn, "s1")
await update_session(conn, "s1", title="My chat")
stale = await get_stale_sessions(conn, timeout_seconds=1800)
old = await get_old_paused_sessions(conn, retention_days=30)
```

## SESSION_COLUMNS contract

Explicit column list for SELECTs.  Schema drift (e.g. new
columns added via migration on an older DB build) surfaces as
a query-time error instead of a silent mismatch — never use
`SELECT *`.

## v4·D audit P1 closure: update_session_status_if

`update_session_status_if` is the atomic CAS variant — uses
`UPDATE ... WHERE status = expected_current` so two concurrent
disconnects on the same session can't both fire
"session.paused" events.  Returns True iff the row was
updated.

## δ2 / H6 (#116): get_old_paused_sessions

The 30-min `get_stale_sessions` check misses the
device-idle-for-a-month scenario because motion-sensor wakeups
refresh `last_active_at` via Tab5
`register→resume→touch_session`.  This long-window query
targets the actually-abandoned case so paused sessions stop
accumulating forever.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

import aiosqlite


# Explicit column list for sessions SELECTs.  Schema drift
# (e.g. new columns added via migration on an older DB build)
# surfaces as a query-time error instead of a silent mismatch.
SESSION_COLUMNS = (
    "id, device_id, type, status, title, system_prompt, config, metadata, "
    "message_count, voice_mode, llm_model, created_at, last_active_at, ended_at"
)


# Allowlist for `update_session` kwargs.  Other fields belong
# to the create / status-transition lifecycle and aren't
# touchable from external callers.
_UPDATE_SESSION_ALLOWED_FIELDS = frozenset({
    "title", "system_prompt", "metadata", "config",
    "voice_mode", "llm_model",
})


async def create_session(
    conn: aiosqlite.Connection,
    *,
    session_id: str,
    device_id: Optional[str] = None,
    session_type: str = "conversation",
    system_prompt: str = "",
    config: Optional[dict] = None,
    voice_mode: int = 0,
    llm_model: str = "",
) -> dict:
    """Create a new session.  Returns the session row as dict.

    `voice_mode` (0-3) and `llm_model` persist the active chat
    v4·C mode onto the session row so the conversation-drawer
    can show its fingerprint and the pipeline can restore it
    on resume.
    """
    now = time.time()
    await conn.execute(
        """
        INSERT INTO sessions (id, device_id, type, status, system_prompt, config,
                              voice_mode, llm_model,
                              created_at, last_active_at)
        VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id, device_id, session_type, system_prompt,
            json.dumps(config or {}),
            int(voice_mode), str(llm_model or ""),
            now, now,
        ),
    )
    await conn.commit()
    return await get_session(conn, session_id)


async def get_session(
    conn: aiosqlite.Connection,
    session_id: str,
) -> Optional[dict]:
    """Fetch a session by ID.  Returns None when not found."""
    cursor = await conn.execute(
        f"SELECT {SESSION_COLUMNS} FROM sessions WHERE id = ?",
        (session_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_sessions(
    conn: aiosqlite.Connection,
    *,
    device_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """List sessions with optional filters and pagination.
    Ordered by `last_active_at` DESC."""
    conditions = []
    params: list[Any] = []

    if device_id:
        conditions.append("device_id = ?")
        params.append(device_id)
    if status:
        conditions.append("status = ?")
        params.append(status)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = (
        f"SELECT {SESSION_COLUMNS} FROM sessions {where} "
        f"ORDER BY last_active_at DESC LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])

    cursor = await conn.execute(query, params)
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def update_session_status(
    conn: aiosqlite.Connection,
    session_id: str,
    status: str,
) -> None:
    """Update session status (active, paused, ended).  Sets
    `ended_at` to now when status='ended'."""
    now = time.time()
    ended_at = now if status == "ended" else None
    await conn.execute(
        """
        UPDATE sessions SET status = ?, last_active_at = ?,
                           ended_at = COALESCE(?, ended_at)
        WHERE id = ?
        """,
        (status, now, ended_at, session_id),
    )
    await conn.commit()


async def update_session_status_if(
    conn: aiosqlite.Connection,
    session_id: str,
    new_status: str,
    expected_current: str,
) -> bool:
    """v4·D audit P1: atomic CAS on session.status.

    Returns True iff the row was updated (i.e., status
    transitioned new_status from expected_current).  Uses a
    single UPDATE ... WHERE so two concurrent disconnects on
    the same session can't both fire "session.paused" events.
    """
    now = time.time()
    ended_at = now if new_status == "ended" else None
    cursor = await conn.execute(
        """
        UPDATE sessions
           SET status = ?, last_active_at = ?,
               ended_at = COALESCE(?, ended_at)
         WHERE id = ? AND status = ?
        """,
        (new_status, now, ended_at, session_id, expected_current),
    )
    await conn.commit()
    return cursor.rowcount > 0


async def touch_session(
    conn: aiosqlite.Connection,
    session_id: str,
) -> None:
    """Update `last_active_at` timestamp."""
    now = time.time()
    await conn.execute(
        "UPDATE sessions SET last_active_at = ? WHERE id = ?",
        (now, session_id),
    )
    await conn.commit()


async def increment_message_count(
    conn: aiosqlite.Connection,
    session_id: str,
) -> None:
    """Increment the denormalized message_count on a session."""
    await conn.execute(
        "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
        (session_id,),
    )
    await conn.commit()


async def update_session(
    conn: aiosqlite.Connection,
    session_id: str,
    **kwargs: Any,
) -> None:
    """Update session fields from a kwargs dict.

    Only fields in `_UPDATE_SESSION_ALLOWED_FIELDS` are
    honoured (title, system_prompt, metadata, config,
    voice_mode, llm_model); others are silently dropped.

    `metadata` + `config` JSON-encoded; `voice_mode` cast to
    int; `llm_model` cast to str (None → "").
    """
    updates = {
        k: v for k, v in kwargs.items()
        if k in _UPDATE_SESSION_ALLOWED_FIELDS
    }
    if not updates:
        return
    now = time.time()
    sets = []
    params: list[Any] = []
    for k, v in updates.items():
        if k in ("metadata", "config"):
            params.append(json.dumps(v))
        elif k == "voice_mode":
            params.append(int(v))
        elif k == "llm_model":
            params.append(str(v or ""))
        else:
            params.append(v)
        sets.append(f"{k} = ?")
    sets.append("last_active_at = ?")
    params.append(now)
    params.append(session_id)
    await conn.execute(
        f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", params,
    )
    await conn.commit()


async def get_stale_sessions(
    conn: aiosqlite.Connection,
    *,
    timeout_seconds: float = 1800,
) -> list[dict]:
    """Find active/paused sessions inactive beyond the timeout.
    Used by SessionManager's cleanup loop to end-session
    stale rows."""
    cutoff = time.time() - timeout_seconds
    cursor = await conn.execute(
        f"""
        SELECT {SESSION_COLUMNS} FROM sessions
        WHERE status IN ('active', 'paused') AND last_active_at < ?
        ORDER BY last_active_at ASC
        """,
        (cutoff,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_old_paused_sessions(
    conn: aiosqlite.Connection,
    *,
    retention_days: int,
) -> list[dict]:
    """Find paused-only sessions older than `retention_days`.

    δ2 / H6 (#116): companion to `get_stale_sessions`.  The
    existing 30-min stale check misses the
    device-idle-for-a-month scenario because motion-sensor
    wakeups refresh last_active_at via Tab5
    register→resume→touch_session.  This long-window query
    targets the actually-abandoned case (no motion-sensor
    pings for ≥ retention_days), so paused sessions stop
    accumulating forever.

    Returns [] when retention_days <= 0 (disabled).
    """
    if retention_days <= 0:
        return []
    cutoff = time.time() - (retention_days * 86400)
    cursor = await conn.execute(
        f"""
        SELECT {SESSION_COLUMNS} FROM sessions
        WHERE status = 'paused' AND last_active_at < ?
        ORDER BY last_active_at ASC
        """,
        (cutoff,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]
