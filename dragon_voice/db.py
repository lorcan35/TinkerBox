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
    # SOLID-audit follow-up (PR #262): message CRUD extracted
    # to db_messages module.  Methods below are thin
    # forwarders so existing call sites keep working unchanged.

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
        from dragon_voice.db_messages import add_message as _impl
        return await _impl(
            self.conn,
            message_id=message_id, session_id=session_id, role=role,
            content=content, input_mode=input_mode, interrupted=interrupted,
            audio_duration_s=audio_duration_s, token_count=token_count,
            model=model, latency_ms=latency_ms,
        )

    async def get_messages(
        self,
        session_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        from dragon_voice.db_messages import get_messages as _impl
        return await _impl(self.conn, session_id, limit=limit, offset=offset)

    async def count_messages(self, session_id: str) -> int:
        from dragon_voice.db_messages import count_messages as _impl
        return await _impl(self.conn, session_id)

    async def get_message(self, message_id: str) -> Optional[dict]:
        from dragon_voice.db_messages import get_message as _impl
        return await _impl(self.conn, message_id)

    async def purge_old_messages(
        self, days: int = 30, batch_size: int = 500,
    ) -> dict[str, int]:
        from dragon_voice.db_messages import purge_old_messages as _impl
        return await _impl(self.conn, days=days, batch_size=batch_size)

    async def delete_messages(self, session_id: str) -> int:
        from dragon_voice.db_messages import delete_messages as _impl
        return await _impl(self.conn, session_id)

    # ── Notes / Events / Config ────────────────────────────────────────
    # SOLID-audit follow-up (PR #263): notes, events, and
    # config-store CRUD extracted to db_notes, db_events,
    # db_config.  Closes audit SRP-7.  Methods below are thin
    # forwarders so existing call sites keep working.

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
        from dragon_voice.db_notes import add_note as _impl
        return await _impl(
            self.conn, note_id=note_id, session_id=session_id,
            title=title, transcript=transcript, summary=summary,
            tags=tags, source=source, duration_s=duration_s,
            word_count=word_count, embedding=embedding,
        )

    async def get_note(self, note_id: str) -> Optional[dict]:
        from dragon_voice.db_notes import get_note as _impl
        return await _impl(self.conn, note_id)

    async def list_notes(
        self, session_id: Optional[str] = None,
        limit: int = 50, offset: int = 0,
    ) -> list[dict]:
        from dragon_voice.db_notes import list_notes as _impl
        return await _impl(
            self.conn, session_id=session_id, limit=limit, offset=offset,
        )

    async def add_event(
        self,
        event_type: str,
        session_id: Optional[str] = None,
        device_id: Optional[str] = None,
        data: Optional[dict] = None,
    ) -> int:
        from dragon_voice.db_events import add_event as _impl
        return await _impl(
            self.conn, event_type,
            session_id=session_id, device_id=device_id, data=data,
        )

    async def get_events(
        self,
        event_type: Optional[str] = None,
        session_id: Optional[str] = None,
        device_id: Optional[str] = None,
        since_id: int = 0,
        limit: int = 100,
    ) -> list[dict]:
        from dragon_voice.db_events import get_events as _impl
        return await _impl(
            self.conn,
            event_type=event_type, session_id=session_id,
            device_id=device_id, since_id=since_id, limit=limit,
        )

    async def get_config(
        self, key: str,
        scope: str = "global", scope_id: Optional[str] = None,
    ) -> Optional[str]:
        from dragon_voice.db_config import get_config as _impl
        return await _impl(self.conn, key, scope=scope, scope_id=scope_id)

    async def set_config(
        self, key: str, value: str,
        scope: str = "global", scope_id: Optional[str] = None,
    ) -> None:
        from dragon_voice.db_config import set_config as _impl
        await _impl(self.conn, key, value, scope=scope, scope_id=scope_id)

    async def get_resolved_config(
        self, key: str,
        device_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Optional[str]:
        from dragon_voice.db_config import get_resolved_config as _impl
        return await _impl(
            self.conn, key, device_id=device_id, session_id=session_id,
        )

    async def list_config(
        self, scope: str = "global", scope_id: Optional[str] = None,
    ) -> dict[str, str]:
        from dragon_voice.db_config import list_config as _impl
        return await _impl(self.conn, scope=scope, scope_id=scope_id)

    async def delete_config(
        self, key: str,
        scope: str = "global", scope_id: Optional[str] = None,
    ) -> bool:
        from dragon_voice.db_config import delete_config as _impl
        return await _impl(self.conn, key, scope=scope, scope_id=scope_id)
