"""Tests for ``dragon_voice.db_messages``.

End-to-end SQL tests against an in-memory aiosqlite connection
with the prod schema.  Pin the W14-H10 batched-purge invariant
+ the side-effect contract that add_message increments the
session counter.
"""
from __future__ import annotations

import time
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from dragon_voice import db_messages, db_sessions

_SCHEMA_PATH = Path(__file__).parent.parent / "schema.sql"


@pytest_asyncio.fixture
async def conn():
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.executescript(_SCHEMA_PATH.read_text())
    await db.commit()
    yield db
    await db.close()


@pytest_asyncio.fixture
async def session_conn(conn):
    """Conn with a pre-created session 's1' for message tests."""
    await db_sessions.create_session(conn, session_id="s1")
    return conn


# ─── add_message ─────────────────────────────────────────────


class TestAddMessage:
    @pytest.mark.asyncio
    async def test_inserts_with_required_fields(self, session_conn):
        msg = await db_messages.add_message(
            session_conn,
            message_id="m1",
            session_id="s1",
            role="user",
            content="hello",
        )
        assert msg["id"] == "m1"
        assert msg["role"] == "user"
        assert msg["content"] == "hello"
        assert msg["interrupted"] == 0

    @pytest.mark.asyncio
    async def test_optional_fields_persisted(self, session_conn):
        msg = await db_messages.add_message(
            session_conn,
            message_id="m1",
            session_id="s1",
            role="assistant",
            content="hi",
            input_mode="voice",
            interrupted=True,
            audio_duration_s=2.5,
            token_count=42,
            model="anthropic/claude-haiku",
            latency_ms=350.0,
        )
        assert msg["input_mode"] == "voice"
        assert msg["interrupted"] == 1
        assert msg["audio_duration_s"] == 2.5
        assert msg["token_count"] == 42
        assert msg["model"] == "anthropic/claude-haiku"
        assert msg["latency_ms"] == 350.0

    @pytest.mark.asyncio
    async def test_increments_session_message_count(self, session_conn):
        """Pin the side-effect contract: add_message MUST
        increment the denormalized counter on the session row."""
        for i in range(3):
            await db_messages.add_message(
                session_conn,
                message_id=f"m{i}", session_id="s1",
                role="user", content=f"msg {i}",
            )
        s = await db_sessions.get_session(session_conn, "s1")
        assert s["message_count"] == 3


# ─── get_messages ────────────────────────────────────────────


class TestGetMessages:
    @pytest.mark.asyncio
    async def test_ordered_ascending_by_created_at(self, session_conn):
        import asyncio
        await db_messages.add_message(
            session_conn, message_id="m1", session_id="s1",
            role="user", content="first",
        )
        await asyncio.sleep(0.01)
        await db_messages.add_message(
            session_conn, message_id="m2", session_id="s1",
            role="assistant", content="second",
        )
        msgs = await db_messages.get_messages(session_conn, "s1")
        assert msgs[0]["content"] == "first"
        assert msgs[1]["content"] == "second"

    @pytest.mark.asyncio
    async def test_pagination(self, session_conn):
        for i in range(5):
            await db_messages.add_message(
                session_conn, message_id=f"m{i}", session_id="s1",
                role="user", content=str(i),
            )
        page1 = await db_messages.get_messages(
            session_conn, "s1", limit=2, offset=0,
        )
        page2 = await db_messages.get_messages(
            session_conn, "s1", limit=2, offset=2,
        )
        assert [m["content"] for m in page1] == ["0", "1"]
        assert [m["content"] for m in page2] == ["2", "3"]

    @pytest.mark.asyncio
    async def test_filter_by_session(self, conn):
        await db_sessions.create_session(conn, session_id="a")
        await db_sessions.create_session(conn, session_id="b")
        await db_messages.add_message(
            conn, message_id="ma1", session_id="a",
            role="user", content="A msg",
        )
        await db_messages.add_message(
            conn, message_id="mb1", session_id="b",
            role="user", content="B msg",
        )

        a_msgs = await db_messages.get_messages(conn, "a")
        b_msgs = await db_messages.get_messages(conn, "b")
        assert len(a_msgs) == 1 and a_msgs[0]["content"] == "A msg"
        assert len(b_msgs) == 1 and b_msgs[0]["content"] == "B msg"


# ─── count_messages / get_message ──────────────────────────


class TestCountAndGet:
    @pytest.mark.asyncio
    async def test_count(self, session_conn):
        assert await db_messages.count_messages(session_conn, "s1") == 0
        for i in range(7):
            await db_messages.add_message(
                session_conn, message_id=f"m{i}", session_id="s1",
                role="user", content=str(i),
            )
        assert await db_messages.count_messages(session_conn, "s1") == 7

    @pytest.mark.asyncio
    async def test_get_returns_none_when_missing(self, session_conn):
        assert await db_messages.get_message(session_conn, "nope") is None


# ─── purge_old_messages ─────────────────────────────────────


class TestPurge:
    @pytest.mark.asyncio
    async def test_disabled_returns_zero(self, session_conn):
        result = await db_messages.purge_old_messages(session_conn, days=0)
        assert result == {"messages": 0, "events": 0}

    @pytest.mark.asyncio
    async def test_purges_old_messages_from_ended_sessions(self, conn):
        # Create an ended session with old messages
        await db_sessions.create_session(conn, session_id="old")
        await db_sessions.update_session_status(conn, "old", "ended")
        await db_messages.add_message(
            conn, message_id="m1", session_id="old",
            role="user", content="old msg",
        )
        # Backdate to 60 days ago
        await conn.execute(
            "UPDATE messages SET created_at = ? WHERE id = ?",
            (time.time() - (60 * 86400), "m1"),
        )
        await conn.commit()

        result = await db_messages.purge_old_messages(conn, days=30)
        assert result["messages"] == 1

    @pytest.mark.asyncio
    async def test_skips_messages_in_active_session(self, conn):
        """W14-H10 / safety pin: messages in active or paused
        sessions MUST NOT be purged even if old (they're in-use
        context)."""
        await db_sessions.create_session(conn, session_id="active")
        await db_messages.add_message(
            conn, message_id="m1", session_id="active",
            role="user", content="old but in active session",
        )
        # Backdate to 60 days ago
        await conn.execute(
            "UPDATE messages SET created_at = ? WHERE id = ?",
            (time.time() - (60 * 86400), "m1"),
        )
        await conn.commit()

        result = await db_messages.purge_old_messages(conn, days=30)
        assert result["messages"] == 0
        # Message still there
        assert await db_messages.get_message(conn, "m1") is not None

    @pytest.mark.asyncio
    async def test_batched_purge_handles_large_set(self, conn):
        """W14-H10 invariant: purge handles N > batch_size by
        looping with yields between batches."""
        await db_sessions.create_session(conn, session_id="ended")
        await db_sessions.update_session_status(conn, "ended", "ended")

        # Insert 12 old messages with batch_size=5 so we exercise
        # the multi-batch loop.
        cutoff = time.time() - (60 * 86400)
        for i in range(12):
            await db_messages.add_message(
                conn, message_id=f"m{i}", session_id="ended",
                role="user", content=str(i),
            )
        await conn.execute(
            "UPDATE messages SET created_at = ?", (cutoff,),
        )
        await conn.commit()

        result = await db_messages.purge_old_messages(
            conn, days=30, batch_size=5,
        )
        assert result["messages"] == 12


# ─── delete_messages ────────────────────────────────────────


class TestDeleteMessages:
    @pytest.mark.asyncio
    async def test_deletes_and_returns_count(self, session_conn):
        for i in range(4):
            await db_messages.add_message(
                session_conn, message_id=f"m{i}", session_id="s1",
                role="user", content=str(i),
            )
        n = await db_messages.delete_messages(session_conn, "s1")
        assert n == 4
        assert await db_messages.count_messages(session_conn, "s1") == 0

    @pytest.mark.asyncio
    async def test_resets_session_message_count_to_zero(self, session_conn):
        for i in range(3):
            await db_messages.add_message(
                session_conn, message_id=f"m{i}", session_id="s1",
                role="user", content=str(i),
            )
        s_before = await db_sessions.get_session(session_conn, "s1")
        assert s_before["message_count"] == 3

        await db_messages.delete_messages(session_conn, "s1")

        s_after = await db_sessions.get_session(session_conn, "s1")
        assert s_after["message_count"] == 0
