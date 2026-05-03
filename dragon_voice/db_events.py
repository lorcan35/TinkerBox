"""Event CRUD — free async functions taking an aiosqlite connection.

Wave 23 SOLID-audit follow-up — thirty-seventh sub-extract.
Sixth slice from `dragon_voice/db.py` (audit SRP-7).

The events table is a generic system-event log: device.connected,
device.disconnected, api_usage, etc.  Each event has a type +
optional session_id + optional device_id + JSON data + auto-
incremented id (used by polling clients via `since_id`).

Pre-extract these were 2 methods on `Database` (~50 LOC).

## API

```python
event_id = await add_event(conn, "device.connected",
                           device_id="d1",
                           data={"firmware_ver": "1.2.3"})
events = await get_events(conn, event_type="api_usage",
                          since_id=last_seen_id, limit=100)
```
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

import aiosqlite


async def add_event(
    conn: aiosqlite.Connection,
    event_type: str,
    *,
    session_id: Optional[str] = None,
    device_id: Optional[str] = None,
    data: Optional[dict] = None,
) -> int:
    """Insert a system event.  Returns the auto-incremented
    event ID.

    `data` is JSON-encoded into the `data` column.  Both
    `session_id` and `device_id` are optional — system-level
    events (e.g. boot, shutdown) may have neither.
    """
    now = time.time()
    cursor = await conn.execute(
        """
        INSERT INTO events (type, session_id, device_id, data, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (event_type, session_id, device_id, json.dumps(data or {}), now),
    )
    await conn.commit()
    return cursor.lastrowid


async def get_events(
    conn: aiosqlite.Connection,
    *,
    event_type: Optional[str] = None,
    session_id: Optional[str] = None,
    device_id: Optional[str] = None,
    since_id: int = 0,
    limit: int = 100,
) -> list[dict]:
    """Get events with optional filters.  Supports polling via
    `since_id` (returns events with `id > since_id`).

    Ordered ASC by id so a polling client can use the last
    returned id as the next since_id and never miss an event.
    """
    conditions = ["id > ?"]
    params: list[Any] = [since_id]

    if event_type:
        conditions.append("type = ?")
        params.append(event_type)
    if session_id:
        conditions.append("session_id = ?")
        params.append(session_id)
    if device_id:
        conditions.append("device_id = ?")
        params.append(device_id)

    where = f"WHERE {' AND '.join(conditions)}"
    cursor = await conn.execute(
        f"SELECT * FROM events {where} ORDER BY id ASC LIMIT ?",
        (*params, limit),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]
