"""Note CRUD — free async functions taking an aiosqlite connection.

Wave 23 SOLID-audit follow-up — thirty-seventh sub-extract.
Fifth slice from `dragon_voice/db.py` (audit SRP-7).

Notes are dictation-derived records (transcript + auto-summary
+ tags + audio metadata).  Pre-extract these were 3 methods on
`Database` (~40 LOC).  Now lives as free functions taking the
connection explicitly.

## API

```python
note = await add_note(conn, note_id="n1", session_id="s1",
                      title="My note", transcript="...")
note = await get_note(conn, "n1")
notes = await list_notes(conn, session_id="s1", limit=50)
```
"""
from __future__ import annotations

import json
import time
from typing import Optional

import aiosqlite


async def add_note(
    conn: aiosqlite.Connection,
    *,
    note_id: str,
    session_id: Optional[str] = None,
    title: str = "",
    transcript: str = "",
    summary: str = "",
    tags: Optional[list[str]] = None,
    source: str = "text",
    duration_s: float = 0.0,
    word_count: int = 0,
    embedding: Optional[bytes] = None,
) -> dict:
    """Insert a note.  Returns the note row as dict.

    `tags` is JSON-encoded.  `embedding` is the raw vector
    bytes (sqlite-vec compatible).  `source` is one of
    "text" / "voice" — used by the dashboard's note-list UI to
    show the right icon.
    """
    now = time.time()
    await conn.execute(
        """
        INSERT INTO notes (id, session_id, title, transcript, summary, tags,
                           source, duration_s, word_count, embedding, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            note_id, session_id, title, transcript, summary,
            json.dumps(tags or []), source, duration_s, word_count,
            embedding, now, now,
        ),
    )
    await conn.commit()

    cursor = await conn.execute(
        "SELECT * FROM notes WHERE id = ?", (note_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else {}


async def get_note(
    conn: aiosqlite.Connection,
    note_id: str,
) -> Optional[dict]:
    """Fetch a note by ID.  Returns None when not found."""
    cursor = await conn.execute(
        "SELECT * FROM notes WHERE id = ?", (note_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_notes(
    conn: aiosqlite.Connection,
    *,
    session_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """List notes, optionally filtered by session, ordered by
    creation time (newest first)."""
    if session_id:
        cursor = await conn.execute(
            "SELECT * FROM notes WHERE session_id = ? "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (session_id, limit, offset),
        )
    else:
        cursor = await conn.execute(
            "SELECT * FROM notes ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]
