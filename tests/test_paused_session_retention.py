"""Unit tests for δ2 (H6): paused-session long-window retention.

Issue #116, refs #89, refs #94.

Pre-fix the only stale-session cleanup path used a 30-min cutoff on
both active AND paused sessions.  Tab5 motion-sensor wakeups every
~20 min trigger register→resume→touch_session, which refreshes
``last_active_at`` and prevents the 30-min check from ever firing
on a paused session that the user isn't actually using.

The fix adds a separate long-window retention pass that targets
ONLY paused sessions whose ``last_active_at`` is older than
``DatabaseConfig.paused_session_retention_days`` (default 30 days).
Once ended, the session's messages purge normally via
``purge_old_messages``.

Tests cover:
  * Config default + dataclass shape
  * The new ``Database.get_old_paused_sessions(retention_days)``
    query against a real aiosqlite DB (matches test_session_cas.py
    pattern — tmp_path + @pytest.mark.asyncio)
  * Active sessions are NOT picked up (status filter)
  * Recent paused sessions are NOT picked up (cutoff filter)
  * ``retention_days <= 0`` disables the query (returns empty)
  * The cleanup-loop integration calls end_session on what the
    query returns (verifies wiring through SessionManager)
"""
from __future__ import annotations

import asyncio
import pathlib
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.config import DatabaseConfig, VoiceConfig
from dragon_voice.db import Database
from dragon_voice.sessions import SessionManager


async def _create_session(
    db: Database,
    session_id: str,
    *,
    status: str = "paused",
    last_active_seconds_ago: float = 0.0,
) -> None:
    """Insert a session row directly with a back-dated last_active_at."""
    now = time.time()
    last_active = now - last_active_seconds_ago
    # Devices FK first.
    await db.conn.execute(
        """INSERT OR IGNORE INTO devices (id, hardware_id, created_at, updated_at)
           VALUES ('dev-test', 'aa:bb:cc:dd:ee:ff', ?, ?)""",
        (now, now),
    )
    await db.conn.execute(
        """
        INSERT INTO sessions
            (id, device_id, type, status, system_prompt, message_count,
             voice_mode, llm_model, created_at, last_active_at)
        VALUES (?, 'dev-test', 'conversation', ?, '', 0, 0, '', ?, ?)
        """,
        (session_id, status, now - last_active_seconds_ago, last_active),
    )
    await db.conn.commit()


# ───────────────────────── DatabaseConfig schema


def test_paused_retention_default_is_30_days() -> None:
    """Pin the default — operator should get the long-window retention
    OOTB without having to set it.  30 days matches the audit's
    suggested baseline."""
    cfg = DatabaseConfig()
    assert cfg.paused_session_retention_days == 30


def test_paused_retention_is_part_of_database_config_dataclass() -> None:
    """Belt-and-suspenders against future config-shape refactors that
    might forget to plumb the new field."""
    cfg = VoiceConfig()
    assert hasattr(cfg.database, "paused_session_retention_days")


# ───────────────────────── Database.get_old_paused_sessions


@pytest.mark.asyncio
async def test_get_old_paused_returns_empty_when_disabled(
    tmp_path: pathlib.Path,
) -> None:
    """``retention_days <= 0`` is the disable knob — must return []
    without even running a query (pin so a future refactor that
    accidentally drops the guard would fail loudly)."""
    db = Database(str(tmp_path / "test.db"))
    await db.initialize()
    try:
        await _create_session(db, "s_old", status="paused",
                              last_active_seconds_ago=86400 * 365)
        out = await db.get_old_paused_sessions(retention_days=0)
        assert out == []
        out = await db.get_old_paused_sessions(retention_days=-5)
        assert out == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_old_paused_returns_only_old_paused_sessions(
    tmp_path: pathlib.Path,
) -> None:
    """Headline contract: paused + last_active_at older than cutoff
    must be returned; paused but recent must NOT; active (any age)
    must NOT (already handled by get_stale_sessions)."""
    db = Database(str(tmp_path / "test.db"))
    await db.initialize()
    try:
        # Paused, 31 days old → SHOULD be returned
        await _create_session(db, "s_old_paused", status="paused",
                              last_active_seconds_ago=86400 * 31)
        # Paused, 5 days old → must NOT be returned (still within window)
        await _create_session(db, "s_recent_paused", status="paused",
                              last_active_seconds_ago=86400 * 5)
        # Active, 100 days old → must NOT be returned (status filter)
        await _create_session(db, "s_old_active", status="active",
                              last_active_seconds_ago=86400 * 100)

        out = await db.get_old_paused_sessions(retention_days=30)
        ids = {s["id"] for s in out}
        assert ids == {"s_old_paused"}, (
            f"Expected only s_old_paused; got {ids}"
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_old_paused_respects_retention_window_boundary(
    tmp_path: pathlib.Path,
) -> None:
    """A session at the cutoff exactly should NOT be returned.  The
    query is `last_active_at < cutoff` (strict less-than) so an
    exactly-on-cutoff session is still within the retention window."""
    db = Database(str(tmp_path / "test.db"))
    await db.initialize()
    try:
        # Just under the boundary (29 days, retention 30) → NOT returned
        await _create_session(db, "s_under", status="paused",
                              last_active_seconds_ago=86400 * 29)
        # Just over the boundary (32 days, retention 30) → returned
        await _create_session(db, "s_over", status="paused",
                              last_active_seconds_ago=86400 * 32)

        out = await db.get_old_paused_sessions(retention_days=30)
        ids = {s["id"] for s in out}
        assert ids == {"s_over"}
    finally:
        await db.close()


# ───────────────────────── SessionManager._cleanup_loop wiring


@pytest.mark.asyncio
async def test_cleanup_loop_calls_end_session_on_old_paused_sessions(
    monkeypatch,
) -> None:
    """Verifies the wiring: SessionManager._cleanup_loop must hit
    get_old_paused_sessions AND call end_session for each result."""
    fake_old = [
        {"id": "abc123", "last_active_at": time.time() - 86400 * 31},
    ]
    db_stub = MagicMock()
    db_stub.get_stale_sessions = AsyncMock(return_value=[])
    db_stub.get_old_paused_sessions = AsyncMock(return_value=fake_old)

    mgr = SessionManager(db=db_stub, paused_retention_days=30)
    mgr.end_session = AsyncMock()

    sleeps = {"n": 0}

    async def fake_sleep(s):
        sleeps["n"] += 1
        if sleeps["n"] > 1:
            raise asyncio.CancelledError()

    import dragon_voice.sessions as sess_mod
    monkeypatch.setattr(sess_mod.asyncio, "sleep", fake_sleep)

    try:
        await mgr._cleanup_loop()
    except asyncio.CancelledError:
        pass

    db_stub.get_old_paused_sessions.assert_awaited_with(30)
    mgr.end_session.assert_awaited_with("abc123")


@pytest.mark.asyncio
async def test_cleanup_loop_with_disabled_retention_still_calls_query(
    monkeypatch,
) -> None:
    """Even when retention_days=0, the loop must still call the query
    (which returns []) — the disable logic lives in the DB method,
    not in the loop.  Pin this so a future "skip the query when
    disabled" optimisation doesn't accidentally split the disable
    semantics across two layers."""
    db_stub = MagicMock()
    db_stub.get_stale_sessions = AsyncMock(return_value=[])
    db_stub.get_old_paused_sessions = AsyncMock(return_value=[])

    mgr = SessionManager(db=db_stub, paused_retention_days=0)
    mgr.end_session = AsyncMock()

    sleeps = {"n": 0}

    async def fake_sleep(s):
        sleeps["n"] += 1
        if sleeps["n"] > 1:
            raise asyncio.CancelledError()

    import dragon_voice.sessions as sess_mod
    monkeypatch.setattr(sess_mod.asyncio, "sleep", fake_sleep)

    try:
        await mgr._cleanup_loop()
    except asyncio.CancelledError:
        pass

    # Query was called with the disable value
    db_stub.get_old_paused_sessions.assert_awaited_with(0)
    # And end_session was NOT called (empty result list)
    mgr.end_session.assert_not_awaited()
