"""Async SQLite database layer for TinkerClaw.

Single module for ALL database access. Uses aiosqlite with WAL mode.
Schema is applied from schema.sql on first run.

refs #16
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

import aiosqlite

logger = logging.getLogger(__name__)

# Default DB path — configurable via TINKERCLAW_DB_PATH env var
DEFAULT_DB_PATH = os.environ.get(
    "TINKERCLAW_DB_PATH", "/home/radxa/tinkerclaw/tinkerclaw.db"
)

# Path to schema.sql (next to this file's repo root)
_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


class Database:
    """Async SQLite database with WAL mode and schema migration."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self._db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        """Open the database, enable WAL + foreign keys, apply schema."""
        # Ensure parent directory exists
        db_dir = os.path.dirname(self._db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row

        # WAL mode + foreign keys
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._db.execute("PRAGMA foreign_keys = ON")

        # Apply schema if tables don't exist
        await self._apply_schema()

        logger.info("Database initialized: %s", self._db_path)

    async def _apply_schema(self) -> None:
        """Read schema.sql and execute it (CREATE IF NOT EXISTS is idempotent)."""
        if not _SCHEMA_PATH.exists():
            logger.warning("schema.sql not found at %s — skipping migration", _SCHEMA_PATH)
            return

        schema_sql = _SCHEMA_PATH.read_text()
        # Split on semicolons and execute each statement
        # (aiosqlite.executescript doesn't return rows, which is fine)
        await self._db.executescript(schema_sql)
        await self._db.commit()
        logger.info("Schema applied from %s", _SCHEMA_PATH)

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None
            logger.info("Database closed")

    @property
    def conn(self) -> aiosqlite.Connection:
        """Raw connection for advanced queries. Prefer typed methods below."""
        if self._db is None:
            raise RuntimeError("Database not initialized — call await db.initialize()")
        return self._db

    # ── Devices ────────────────────────────────────────────────────────

    async def upsert_device(
        self,
        device_id: str,
        hardware_id: str,
        name: str = "",
        firmware_ver: str = "",
        platform: str = "",
        capabilities: Optional[dict] = None,
    ) -> dict:
        """Register or update a device. Returns the device row as dict."""
        now = time.time()
        caps_json = json.dumps(capabilities or {})

        await self.conn.execute(
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
        await self.conn.commit()
        return await self.get_device(device_id)

    async def get_device(self, device_id: str) -> Optional[dict]:
        """Fetch a device by ID."""
        cursor = await self.conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_devices(self, online_only: bool = False) -> list[dict]:
        """List all devices, optionally filtered to online-only."""
        if online_only:
            cursor = await self.conn.execute(
                "SELECT * FROM devices WHERE is_online = 1 ORDER BY last_seen_at DESC"
            )
        else:
            cursor = await self.conn.execute(
                "SELECT * FROM devices ORDER BY last_seen_at DESC"
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def set_device_online(self, device_id: str, online: bool) -> None:
        """Mark a device as online or offline."""
        now = time.time()
        await self.conn.execute(
            "UPDATE devices SET is_online = ?, last_seen_at = ?, updated_at = ? WHERE id = ?",
            (1 if online else 0, now, now, device_id),
        )
        await self.conn.commit()

    async def update_device(self, device_id: str, **kwargs) -> None:
        """Update device fields. Allowed: name, config."""
        allowed = {"name", "config"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        now = time.time()
        sets = []
        params = []
        for k, v in updates.items():
            sets.append(f"{k} = ?")
            params.append(json.dumps(v) if k == "config" else v)
        sets.append("updated_at = ?")
        params.append(now)
        params.append(device_id)
        await self.conn.execute(
            f"UPDATE devices SET {', '.join(sets)} WHERE id = ?", params
        )
        await self.conn.commit()

    async def delete_device(self, device_id: str) -> None:
        """Delete a device. Sessions with this device get device_id=NULL (FK ON DELETE SET NULL)."""
        await self.conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        await self.conn.commit()

    # ── Sessions ───────────────────────────────────────────────────────

    async def create_session(
        self,
        session_id: str,
        device_id: Optional[str] = None,
        session_type: str = "conversation",
        system_prompt: str = "",
        config: Optional[dict] = None,
    ) -> dict:
        """Create a new session. Returns the session row as dict."""
        now = time.time()
        await self.conn.execute(
            """
            INSERT INTO sessions (id, device_id, type, status, system_prompt, config,
                                  created_at, last_active_at)
            VALUES (?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            (session_id, device_id, session_type, system_prompt,
             json.dumps(config or {}), now, now),
        )
        await self.conn.commit()
        return await self.get_session(session_id)

    async def get_session(self, session_id: str) -> Optional[dict]:
        """Fetch a session by ID."""
        cursor = await self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_sessions(
        self,
        device_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """List sessions with optional filters and pagination."""
        conditions = []
        params: list[Any] = []

        if device_id:
            conditions.append("device_id = ?")
            params.append(device_id)
        if status:
            conditions.append("status = ?")
            params.append(status)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"SELECT * FROM sessions {where} ORDER BY last_active_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor = await self.conn.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def update_session_status(
        self, session_id: str, status: str
    ) -> None:
        """Update session status (active, paused, ended)."""
        now = time.time()
        ended_at = now if status == "ended" else None
        await self.conn.execute(
            """
            UPDATE sessions SET status = ?, last_active_at = ?,
                               ended_at = COALESCE(?, ended_at)
            WHERE id = ?
            """,
            (status, now, ended_at, session_id),
        )
        await self.conn.commit()

    async def touch_session(self, session_id: str) -> None:
        """Update last_active_at timestamp."""
        now = time.time()
        await self.conn.execute(
            "UPDATE sessions SET last_active_at = ? WHERE id = ?",
            (now, session_id),
        )
        await self.conn.commit()

    async def increment_message_count(self, session_id: str) -> None:
        """Increment the denormalized message_count on a session."""
        await self.conn.execute(
            "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
            (session_id,),
        )
        await self.conn.commit()

    async def update_session(self, session_id: str, **kwargs) -> None:
        """Update session fields. Allowed: title, system_prompt, metadata, config."""
        allowed = {"title", "system_prompt", "metadata", "config"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        now = time.time()
        sets = []
        params = []
        for k, v in updates.items():
            sets.append(f"{k} = ?")
            params.append(json.dumps(v) if k in ("metadata", "config") else v)
        sets.append("last_active_at = ?")
        params.append(now)
        params.append(session_id)
        await self.conn.execute(
            f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", params
        )
        await self.conn.commit()

    async def get_stale_sessions(self, timeout_seconds: float = 1800) -> list[dict]:
        """Find active/paused sessions inactive beyond the timeout."""
        cutoff = time.time() - timeout_seconds
        cursor = await self.conn.execute(
            """
            SELECT * FROM sessions
            WHERE status IN ('active', 'paused') AND last_active_at < ?
            ORDER BY last_active_at ASC
            """,
            (cutoff,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ── Messages ───────────────────────────────────────────────────────

    async def add_message(
        self,
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
        """Insert an append-only message. Returns the message row as dict."""
        now = time.time()
        await self.conn.execute(
            """
            INSERT INTO messages (id, session_id, role, content, input_mode, interrupted,
                                  audio_duration_s, token_count, model, latency_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (message_id, session_id, role, content, input_mode,
             1 if interrupted else 0, audio_duration_s, token_count,
             model, latency_ms, now),
        )
        await self.conn.commit()

        # Update denormalized count
        await self.increment_message_count(session_id)

        cursor = await self.conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,))
        row = await cursor.fetchone()
        return dict(row) if row else {}

    async def get_messages(
        self,
        session_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Get messages for a session, ordered by creation time (ascending)."""
        cursor = await self.conn.execute(
            """
            SELECT * FROM messages WHERE session_id = ?
            ORDER BY created_at ASC LIMIT ? OFFSET ?
            """,
            (session_id, limit, offset),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def count_messages(self, session_id: str) -> int:
        """Count messages in a session."""
        cursor = await self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_message(self, message_id: str) -> Optional[dict]:
        """Fetch a single message by ID."""
        cursor = await self.conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def delete_messages(self, session_id: str) -> int:
        """Delete all messages for a session. Returns count deleted."""
        count = await self.count_messages(session_id)
        await self.conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        # Reset denormalized count
        await self.conn.execute(
            "UPDATE sessions SET message_count = 0 WHERE id = ?", (session_id,)
        )
        await self.conn.commit()
        return count

    # ── Notes ──────────────────────────────────────────────────────────

    async def add_note(
        self,
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
        """Insert a note. Returns the note row as dict."""
        now = time.time()
        await self.conn.execute(
            """
            INSERT INTO notes (id, session_id, title, transcript, summary, tags,
                               source, duration_s, word_count, embedding, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (note_id, session_id, title, transcript, summary,
             json.dumps(tags or []), source, duration_s, word_count,
             embedding, now, now),
        )
        await self.conn.commit()

        cursor = await self.conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,))
        row = await cursor.fetchone()
        return dict(row) if row else {}

    async def get_note(self, note_id: str) -> Optional[dict]:
        """Fetch a note by ID."""
        cursor = await self.conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_notes(
        self, session_id: Optional[str] = None, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        """List notes, optionally filtered by session."""
        if session_id:
            cursor = await self.conn.execute(
                "SELECT * FROM notes WHERE session_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (session_id, limit, offset),
            )
        else:
            cursor = await self.conn.execute(
                "SELECT * FROM notes ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ── Events ─────────────────────────────────────────────────────────

    async def add_event(
        self,
        event_type: str,
        session_id: Optional[str] = None,
        device_id: Optional[str] = None,
        data: Optional[dict] = None,
    ) -> int:
        """Insert a system event. Returns the auto-incremented event ID."""
        now = time.time()
        cursor = await self.conn.execute(
            """
            INSERT INTO events (type, session_id, device_id, data, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event_type, session_id, device_id, json.dumps(data or {}), now),
        )
        await self.conn.commit()
        return cursor.lastrowid

    async def get_events(
        self,
        event_type: Optional[str] = None,
        session_id: Optional[str] = None,
        device_id: Optional[str] = None,
        since_id: int = 0,
        limit: int = 100,
    ) -> list[dict]:
        """Get events with optional filters. Supports polling via since_id."""
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
        cursor = await self.conn.execute(
            f"SELECT * FROM events {where} ORDER BY id ASC LIMIT ?",
            (*params, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ── Config Store ───────────────────────────────────────────────────

    async def get_config(
        self,
        key: str,
        scope: str = "global",
        scope_id: Optional[str] = None,
    ) -> Optional[str]:
        """Get a config value. Returns JSON-encoded string or None."""
        cursor = await self.conn.execute(
            "SELECT value FROM config WHERE key = ? AND scope = ? AND scope_id IS ?",
            (key, scope, scope_id),
        )
        row = await cursor.fetchone()
        return row["value"] if row else None

    async def set_config(
        self,
        key: str,
        value: str,
        scope: str = "global",
        scope_id: Optional[str] = None,
    ) -> None:
        """Set a config value (upsert). Value should be JSON-encoded."""
        now = time.time()
        await self.conn.execute(
            """
            INSERT INTO config (key, value, scope, scope_id, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(key, scope, scope_id) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, scope, scope_id, now),
        )
        await self.conn.commit()

    async def get_resolved_config(self, key: str, device_id: Optional[str] = None,
                                   session_id: Optional[str] = None) -> Optional[str]:
        """Get config with scope resolution: session > device > global."""
        # Try session scope first
        if session_id:
            val = await self.get_config(key, "session", session_id)
            if val is not None:
                return val
        # Then device scope
        if device_id:
            val = await self.get_config(key, "device", device_id)
            if val is not None:
                return val
        # Fall back to global
        return await self.get_config(key, "global")

    async def list_config(self, scope: str = "global", scope_id: Optional[str] = None) -> dict[str, str]:
        """List all config entries for a given scope."""
        cursor = await self.conn.execute(
            "SELECT key, value FROM config WHERE scope = ? AND scope_id IS ?",
            (scope, scope_id),
        )
        rows = await cursor.fetchall()
        return {row["key"]: row["value"] for row in rows}

    async def delete_config(self, key: str, scope: str = "global",
                            scope_id: Optional[str] = None) -> bool:
        """Delete a config key. Returns True if a row was deleted."""
        cursor = await self.conn.execute(
            "DELETE FROM config WHERE key = ? AND scope = ? AND scope_id IS ?",
            (key, scope, scope_id),
        )
        await self.conn.commit()
        return cursor.rowcount > 0
