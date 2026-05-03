"""Device CRUD — free async functions taking an aiosqlite connection.

Wave 23 SOLID-audit follow-up — thirty-fourth sub-extract.
Second slice from `dragon_voice/db.py` (audit SRP-7: 844-LOC
`Database` class is a monolith of 6+ method-domain mixins;
this extracts the device CRUD).

Pre-extract these were 6 methods on `Database` (~80 LOC of
SQL).  Now lives as free functions taking the connection
explicitly.  The `Database` methods become thin forwarders so
all existing call sites keep working unchanged.

## API

```python
device = await upsert_device(
    conn, device_id="tab5-7", hardware_id="abc",
    name="Living room", firmware_ver="1.2.3",
    platform="esp32-p4",
    capabilities={"widgets": {"types": ["live"]}},
)
device = await get_device(conn, "tab5-7")
devices = await list_devices(conn, online_only=False)
await set_device_online(conn, "tab5-7", True)
await update_device(conn, "tab5-7", name="Bedroom")
await delete_device(conn, "tab5-7")
```

## Why free functions instead of a mixin class

Mixin classes share state with the parent and obscure the
dependency surface.  Free functions taking the connection
explicitly are testable in isolation (just hand them an
aiosqlite connection — no Database instance needed) and the
SQL surface is greppable to one file.

## Behaviour preserved verbatim

* `upsert_device` ON CONFLICT clause preserves existing values
  for fields that arrive as empty strings (CASE WHEN excluded
  pattern) — pre-extract intent: a re-register frame from
  Tab5 firmware that omits `name` shouldn't blank the user-
  set name in the dashboard.
* `update_device` allowlist (`{name, config}`) — only those
  two fields can be updated externally; other fields belong
  to the registration / online lifecycle.
* `delete_device` foreign-key behaviour: sessions get
  device_id=NULL via the schema's ON DELETE SET NULL; we
  don't cascade-delete sessions because they're history.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

import aiosqlite


async def upsert_device(
    conn: aiosqlite.Connection,
    *,
    device_id: str,
    hardware_id: str,
    name: str = "",
    firmware_ver: str = "",
    platform: str = "",
    capabilities: Optional[dict] = None,
) -> dict:
    """Register or update a device.  Returns the device row as
    a dict.

    The ON CONFLICT clause preserves existing values for fields
    that arrive as empty strings — a re-register frame from
    Tab5 firmware that omits `name` won't blank the user-set
    name in the dashboard.

    `capabilities` is JSON-encoded into the column; pass `None`
    or an empty dict to leave the existing value alone (the
    `excluded.capabilities != '{}'` guard preserves prior caps).
    """
    now = time.time()
    caps_json = json.dumps(capabilities or {})

    await conn.execute(
        """
        INSERT INTO devices (id, hardware_id, name, firmware_ver, platform,
                             capabilities, is_online, last_seen_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            hardware_id = excluded.hardware_id,
            name = CASE WHEN excluded.name != '' THEN excluded.name ELSE devices.name END,
            firmware_ver = CASE WHEN excluded.firmware_ver != '' THEN excluded.firmware_ver ELSE devices.firmware_ver END,
            platform = CASE WHEN excluded.platform != '' THEN excluded.platform ELSE devices.platform END,
            capabilities = CASE WHEN excluded.capabilities != '{}' THEN excluded.capabilities ELSE devices.capabilities END,
            is_online = 1,
            last_seen_at = excluded.last_seen_at,
            updated_at = excluded.updated_at
        """,
        (device_id, hardware_id, name, firmware_ver, platform, caps_json, now, now, now),
    )
    await conn.commit()
    return await get_device(conn, device_id)


async def get_device(
    conn: aiosqlite.Connection,
    device_id: str,
) -> Optional[dict]:
    """Fetch a device by ID.  Returns None when not found."""
    cursor = await conn.execute(
        "SELECT * FROM devices WHERE id = ?", (device_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_devices(
    conn: aiosqlite.Connection,
    *,
    online_only: bool = False,
) -> list[dict]:
    """List all devices, ordered by `last_seen_at` DESC.

    `online_only=True` filters to `is_online = 1` (used by the
    dashboard "active devices" panel).
    """
    if online_only:
        cursor = await conn.execute(
            "SELECT * FROM devices WHERE is_online = 1 ORDER BY last_seen_at DESC",
        )
    else:
        cursor = await conn.execute(
            "SELECT * FROM devices ORDER BY last_seen_at DESC",
        )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def set_device_online(
    conn: aiosqlite.Connection,
    device_id: str,
    online: bool,
) -> None:
    """Mark a device as online or offline.  Updates the
    `last_seen_at` and `updated_at` timestamps too."""
    now = time.time()
    await conn.execute(
        "UPDATE devices SET is_online = ?, last_seen_at = ?, updated_at = ? WHERE id = ?",
        (1 if online else 0, now, now, device_id),
    )
    await conn.commit()


# Allowlist of fields that can be updated via `update_device`.
# Other fields (id, hardware_id, registration timestamps, etc.)
# are owned by the registration / lifecycle flow and shouldn't
# be touchable from external callers.
_UPDATE_DEVICE_ALLOWED_FIELDS = frozenset({"name", "config"})


async def update_device(
    conn: aiosqlite.Connection,
    device_id: str,
    **kwargs: Any,
) -> None:
    """Update device fields from a kwargs dict.

    Only fields in `_UPDATE_DEVICE_ALLOWED_FIELDS` are honoured;
    others are silently dropped (they belong to the registration
    or online-lifecycle flows and aren't user-editable).
    """
    updates = {
        k: v for k, v in kwargs.items()
        if k in _UPDATE_DEVICE_ALLOWED_FIELDS
    }
    if not updates:
        return
    now = time.time()
    sets = []
    params: list[Any] = []
    for k, v in updates.items():
        sets.append(f"{k} = ?")
        params.append(json.dumps(v) if k == "config" else v)
    sets.append("updated_at = ?")
    params.append(now)
    params.append(device_id)
    await conn.execute(
        f"UPDATE devices SET {', '.join(sets)} WHERE id = ?", params,
    )
    await conn.commit()


async def delete_device(
    conn: aiosqlite.Connection,
    device_id: str,
) -> None:
    """Delete a device.  Sessions belonging to this device get
    their `device_id` set to NULL via the schema's
    `ON DELETE SET NULL` foreign-key constraint — we don't
    cascade-delete sessions because they're history."""
    await conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
    await conn.commit()
