"""Async SQLite database layer for TinkerClaw.

Single module for ALL database access. Uses aiosqlite with WAL mode.
Schema is applied from schema.sql on first run.

refs #16
"""

import asyncio
import json
import logging
import os
import sqlite3
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
        """Open the database, enable WAL + foreign keys, apply schema.

        If the database or WAL file is corrupt (e.g. power loss during
        checkpoint), attempts automatic recovery by renaming the corrupt
        files and retrying.  Corrupt files are preserved with a .corrupt
        suffix for manual data recovery.
        """
        # Ensure parent directory exists
        db_dir = os.path.dirname(self._db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        try:
            self._db = await aiosqlite.connect(self._db_path)
            self._db.row_factory = aiosqlite.Row

            # WAL mode + foreign keys — this is where corruption surfaces
            await self._db.execute("PRAGMA journal_mode = WAL")
            await self._db.execute("PRAGMA foreign_keys = ON")
            # Reduce eMMC write amplification (DQ04):
            # - synchronous=NORMAL skips fsync on WAL writes (safe in WAL mode,
            #   only risks losing last transaction on OS crash, not corruption)
            # - wal_autocheckpoint=1000 is the SQLite default (1000 pages ~4MB),
            #   set explicitly to prevent any library from lowering it
            await self._db.execute("PRAGMA synchronous = NORMAL")
            await self._db.execute("PRAGMA wal_autocheckpoint = 1000")
            # Quick integrity probe: read from sqlite_master
            await self._db.execute("SELECT count(*) FROM sqlite_master")
        except (sqlite3.DatabaseError, sqlite3.OperationalError) as exc:
            exc_str = str(exc).lower()
            if "corrupt" in exc_str or "malformed" in exc_str or "not a database" in exc_str:
                logger.error(
                    "Database corruption detected during startup: %s — attempting recovery",
                    exc,
                )
                await self._recover_corrupt_db()
                # Retry open after recovery
                self._db = await aiosqlite.connect(self._db_path)
                self._db.row_factory = aiosqlite.Row
                await self._db.execute("PRAGMA journal_mode = WAL")
                await self._db.execute("PRAGMA foreign_keys = ON")
                await self._db.execute("PRAGMA synchronous = NORMAL")
                await self._db.execute("PRAGMA wal_autocheckpoint = 1000")
            else:
                raise

        # Apply schema if tables don't exist
        await self._apply_schema()

        logger.info("Database initialized: %s", self._db_path)

    async def _recover_corrupt_db(self) -> None:
        """Attempt to recover from a corrupt database file.

        SOLID-audit follow-up: implementation extracted to
        db_corruption_recovery.recover_corrupt_db.  This
        wrapper resets `self._db` to None (the caller's
        re-open path expects it) and forwards to the free
        function.
        """
        from dragon_voice.db_corruption_recovery import recover_corrupt_db

        prev_conn = self._db
        self._db = None  # caller re-opens via aiosqlite.connect
        await recover_corrupt_db(self._db_path, prev_conn)

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
        """Close the database connection.

        v4·D audit P2 fix: checkpoint the WAL before closing so the next
        startup doesn't have to replay a long tail of uncommitted pages.
        PRAGMA wal_checkpoint(TRUNCATE) both merges + truncates the WAL.
        """
        if self._db:
            try:
                await self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                logger.debug("wal_checkpoint on close failed", exc_info=True)
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
    # SOLID-audit follow-up (PR #260): device CRUD extracted to
    # db_devices module.  Methods below are thin forwarders so
    # all existing call sites keep working unchanged.

    async def upsert_device(
        self,
        device_id: str,
        hardware_id: str,
        name: str = "",
        firmware_ver: str = "",
        platform: str = "",
        capabilities: Optional[dict] = None,
    ) -> dict:
        from dragon_voice.db_devices import upsert_device as _impl
        return await _impl(
            self.conn,
            device_id=device_id, hardware_id=hardware_id,
            name=name, firmware_ver=firmware_ver, platform=platform,
            capabilities=capabilities,
        )

    async def get_device(self, device_id: str) -> Optional[dict]:
        from dragon_voice.db_devices import get_device as _impl
        return await _impl(self.conn, device_id)

    async def list_devices(self, online_only: bool = False) -> list[dict]:
        from dragon_voice.db_devices import list_devices as _impl
        return await _impl(self.conn, online_only=online_only)

    async def set_device_online(self, device_id: str, online: bool) -> None:
        from dragon_voice.db_devices import set_device_online as _impl
        await _impl(self.conn, device_id, online)

    async def update_device(self, device_id: str, **kwargs) -> None:
        from dragon_voice.db_devices import update_device as _impl
        await _impl(self.conn, device_id, **kwargs)

    async def delete_device(self, device_id: str) -> None:
        from dragon_voice.db_devices import delete_device as _impl
        await _impl(self.conn, device_id)

    # ── Sessions ───────────────────────────────────────────────────────
    # SOLID-audit follow-up (PR #261): session CRUD extracted
    # to db_sessions module.  Methods below are thin
    # forwarders so existing call sites keep working unchanged.

    # Re-exposed for backward compat with anything that
    # imported `Database._SESSION_COLUMNS` directly.
    from dragon_voice.db_sessions import SESSION_COLUMNS as _SESSION_COLUMNS  # noqa: E402

    async def create_session(
        self,
        session_id: str,
        device_id: Optional[str] = None,
        session_type: str = "conversation",
        system_prompt: str = "",
        config: Optional[dict] = None,
        voice_mode: int = 0,
        llm_model: str = "",
    ) -> dict:
        from dragon_voice.db_sessions import create_session as _impl
        return await _impl(
            self.conn,
            session_id=session_id, device_id=device_id,
            session_type=session_type, system_prompt=system_prompt,
            config=config, voice_mode=voice_mode, llm_model=llm_model,
        )

    async def get_session(self, session_id: str) -> Optional[dict]:
        from dragon_voice.db_sessions import get_session as _impl
        return await _impl(self.conn, session_id)

    async def list_sessions(
        self,
        device_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        from dragon_voice.db_sessions import list_sessions as _impl
        return await _impl(
            self.conn, device_id=device_id, status=status,
            limit=limit, offset=offset,
        )

    async def update_session_status(
        self, session_id: str, status: str,
    ) -> None:
        from dragon_voice.db_sessions import update_session_status as _impl
        await _impl(self.conn, session_id, status)

    async def update_session_status_if(
        self, session_id: str, new_status: str, expected_current: str,
    ) -> bool:
        from dragon_voice.db_sessions import update_session_status_if as _impl
        return await _impl(
            self.conn, session_id, new_status, expected_current,
        )

    async def touch_session(self, session_id: str) -> None:
        from dragon_voice.db_sessions import touch_session as _impl
        await _impl(self.conn, session_id)

    async def increment_message_count(self, session_id: str) -> None:
        from dragon_voice.db_sessions import increment_message_count as _impl
        await _impl(self.conn, session_id)

    async def update_session(self, session_id: str, **kwargs) -> None:
        from dragon_voice.db_sessions import update_session as _impl
        await _impl(self.conn, session_id, **kwargs)

    async def get_stale_sessions(
        self, timeout_seconds: float = 1800,
    ) -> list[dict]:
        from dragon_voice.db_sessions import get_stale_sessions as _impl
        return await _impl(self.conn, timeout_seconds=timeout_seconds)

    async def get_old_paused_sessions(
        self, retention_days: int,
    ) -> list[dict]:
        from dragon_voice.db_sessions import get_old_paused_sessions as _impl
        return await _impl(self.conn, retention_days=retention_days)

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

    async def purge_old_messages(
        self, days: int = 30, batch_size: int = 500
    ) -> dict[str, int]:
        """Purge messages and orphaned events older than `days`.

        Skips messages belonging to active or paused sessions to avoid
        deleting context from sessions still in use.

        Wave 14 W14-H10: previously a single ``DELETE ... WHERE NOT IN
        (SELECT ...)`` ran on the shared aiosqlite connection — on a
        large tail it serialized every other coroutine behind 2-3 s of
        purge. Now the delete runs in ``batch_size``-row chunks with
        ``await asyncio.sleep(0)`` between batches, yielding the event
        loop so WS keepalives and receipt emits can progress even on a
        huge purge.

        Returns dict with counts: {"messages": N, "events": M}.
        """
        if days <= 0:
            logger.info("Message purge disabled (days=%d)", days)
            return {"messages": 0, "events": 0}

        cutoff = time.time() - (days * 86400)

        # Delete old messages, but only from ended sessions (or sessions
        # with no matching row, i.e. orphaned messages).
        # Wave 14 W14-H10: batch via LIMIT + asyncio.sleep(0) yield so
        # the purge does NOT hold the aiosqlite background thread
        # exclusively for the full delete.
        msg_count = 0
        while True:
            cursor = await self.conn.execute(
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
            await self.conn.commit()
            msg_count += deleted
            if deleted < batch_size:
                break
            await asyncio.sleep(0)  # yield the loop between batches

        # Delete orphaned events older than the cutoff — same batching.
        evt_count = 0
        while True:
            cursor = await self.conn.execute(
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
            await self.conn.commit()
            evt_count += deleted
            if deleted < batch_size:
                break
            await asyncio.sleep(0)

        # PASSIVE checkpoint after bulk deletes — moves WAL pages back into
        # the main DB file without blocking readers. Reduces WAL file growth
        # and consolidates writes to reduce eMMC wear (DQ04).
        if msg_count > 0 or evt_count > 0:
            try:
                await self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                logger.info("WAL passive checkpoint after purge")
            except Exception as ckpt_err:
                logger.warning("WAL checkpoint after purge failed: %s", ckpt_err)

        logger.info(
            "Purged %d messages and %d events older than %d days",
            msg_count, evt_count, days,
        )
        return {"messages": msg_count, "events": evt_count}

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
