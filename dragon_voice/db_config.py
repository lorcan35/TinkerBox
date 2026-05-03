"""Config-store CRUD with scope-resolution — free async functions
taking an aiosqlite connection.

Wave 23 SOLID-audit follow-up — thirty-seventh sub-extract.
Seventh slice from `dragon_voice/db.py` (audit SRP-7) — final
domain mixin extract that closes SRP-7.

The config table is a key/value store with three scopes:
  * `global` — applies to all sessions/devices
  * `device` — keyed by `scope_id = device_id`
  * `session` — keyed by `scope_id = session_id`

`get_resolved_config` walks the scope priority chain
(session > device > global) so a session-specific override
shadows the device default which shadows the global default.

Pre-extract these were 5 methods on `Database` (~75 LOC).

## API

```python
await set_config(conn, "voice_mode", "1", scope="device", scope_id="d1")
val = await get_config(conn, "voice_mode", scope="device", scope_id="d1")
val = await get_resolved_config(conn, "voice_mode",
                                device_id="d1", session_id="s1")
all_global = await list_config(conn, scope="global")
existed = await delete_config(conn, "voice_mode",
                              scope="device", scope_id="d1")
```
"""
from __future__ import annotations

import time
from typing import Optional

import aiosqlite


async def get_config(
    conn: aiosqlite.Connection,
    key: str,
    *,
    scope: str = "global",
    scope_id: Optional[str] = None,
) -> Optional[str]:
    """Get a config value.  Returns the JSON-encoded string or
    None if not present.

    `scope_id` is None for global scope (the SQL uses `IS ?`
    so NULL matches NULL on the unique index).
    """
    cursor = await conn.execute(
        "SELECT value FROM config WHERE key = ? AND scope = ? AND scope_id IS ?",
        (key, scope, scope_id),
    )
    row = await cursor.fetchone()
    return row["value"] if row else None


async def set_config(
    conn: aiosqlite.Connection,
    key: str,
    value: str,
    *,
    scope: str = "global",
    scope_id: Optional[str] = None,
) -> None:
    """Set a config value (upsert).  `value` should be a JSON-
    encoded string — the caller owns the encoding so the same
    column can hold strings, numbers, dicts, etc."""
    now = time.time()
    await conn.execute(
        """
        INSERT INTO config (key, value, scope, scope_id, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(key, scope, scope_id) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (key, value, scope, scope_id, now),
    )
    await conn.commit()


async def get_resolved_config(
    conn: aiosqlite.Connection,
    key: str,
    *,
    device_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Optional[str]:
    """Get config with scope resolution: session > device >
    global.  Returns the most-specific scope's value, or None
    when the key isn't set anywhere."""
    if session_id:
        val = await get_config(conn, key, scope="session", scope_id=session_id)
        if val is not None:
            return val
    if device_id:
        val = await get_config(conn, key, scope="device", scope_id=device_id)
        if val is not None:
            return val
    return await get_config(conn, key, scope="global")


async def list_config(
    conn: aiosqlite.Connection,
    *,
    scope: str = "global",
    scope_id: Optional[str] = None,
) -> dict[str, str]:
    """List all config entries for a given scope.  Returns
    {key: value} dict (values are still JSON-encoded strings —
    caller decodes)."""
    cursor = await conn.execute(
        "SELECT key, value FROM config WHERE scope = ? AND scope_id IS ?",
        (scope, scope_id),
    )
    rows = await cursor.fetchall()
    return {row["key"]: row["value"] for row in rows}


async def delete_config(
    conn: aiosqlite.Connection,
    key: str,
    *,
    scope: str = "global",
    scope_id: Optional[str] = None,
) -> bool:
    """Delete a config key.  Returns True iff a row was actually
    deleted (False when the key didn't exist at that scope)."""
    cursor = await conn.execute(
        "DELETE FROM config WHERE key = ? AND scope = ? AND scope_id IS ?",
        (key, scope, scope_id),
    )
    await conn.commit()
    return cursor.rowcount > 0
