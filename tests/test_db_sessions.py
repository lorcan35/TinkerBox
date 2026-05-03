"""Tests for ``dragon_voice.db_sessions``.

End-to-end SQL tests against an in-memory aiosqlite connection
with the prod schema.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from dragon_voice import db_sessions

_SCHEMA_PATH = Path(__file__).parent.parent / "schema.sql"


@pytest_asyncio.fixture
async def conn():
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.executescript(_SCHEMA_PATH.read_text())
    await db.commit()
    yield db
    await db.close()


# ─── create_session ──────────────────────────────────────────


class TestCreate:
    @pytest.mark.asyncio
    async def test_creates_session_with_defaults(self, conn):
        s = await db_sessions.create_session(conn, session_id="s1")
        assert s["id"] == "s1"
        assert s["status"] == "active"
        assert s["voice_mode"] == 0
        assert s["llm_model"] == ""
        assert s["message_count"] == 0

    @pytest.mark.asyncio
    async def test_creates_with_voice_mode_and_llm_model(self, conn):
        s = await db_sessions.create_session(
            conn, session_id="s1",
            voice_mode=2,
            llm_model="anthropic/claude-haiku",
        )
        assert s["voice_mode"] == 2
        assert s["llm_model"] == "anthropic/claude-haiku"

    @pytest.mark.asyncio
    async def test_config_json_encoded(self, conn):
        await db_sessions.create_session(
            conn, session_id="s1",
            config={"key": "value", "n": 42},
        )
        s = await db_sessions.get_session(conn, "s1")
        assert json.loads(s["config"]) == {"key": "value", "n": 42}


# ─── list_sessions ──────────────────────────────────────────


class TestList:
    @pytest.mark.asyncio
    async def test_filter_by_device_id(self, conn):
        # FK constraint: devices must exist before sessions
        from dragon_voice import db_devices
        await db_devices.upsert_device(conn, device_id="d1", hardware_id="h1")
        await db_devices.upsert_device(conn, device_id="d2", hardware_id="h2")

        await db_sessions.create_session(conn, session_id="a", device_id="d1")
        await db_sessions.create_session(conn, session_id="b", device_id="d2")

        result = await db_sessions.list_sessions(conn, device_id="d1")
        assert {s["id"] for s in result} == {"a"}

    @pytest.mark.asyncio
    async def test_filter_by_status(self, conn):
        await db_sessions.create_session(conn, session_id="a")
        await db_sessions.create_session(conn, session_id="b")
        await db_sessions.update_session_status(conn, "b", "ended")

        active = await db_sessions.list_sessions(conn, status="active")
        ended = await db_sessions.list_sessions(conn, status="ended")
        assert {s["id"] for s in active} == {"a"}
        assert {s["id"] for s in ended} == {"b"}

    @pytest.mark.asyncio
    async def test_pagination(self, conn):
        for i in range(5):
            await db_sessions.create_session(conn, session_id=f"s{i}")

        page1 = await db_sessions.list_sessions(conn, limit=2, offset=0)
        page2 = await db_sessions.list_sessions(conn, limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2

    @pytest.mark.asyncio
    async def test_ordered_by_last_active_desc(self, conn):
        await db_sessions.create_session(conn, session_id="first")
        await asyncio.sleep(0.01)
        await db_sessions.create_session(conn, session_id="second")

        result = await db_sessions.list_sessions(conn)
        assert result[0]["id"] == "second"
        assert result[1]["id"] == "first"


# ─── status transitions ─────────────────────────────────────


class TestStatusTransitions:
    @pytest.mark.asyncio
    async def test_update_status_paused(self, conn):
        await db_sessions.create_session(conn, session_id="s1")
        await db_sessions.update_session_status(conn, "s1", "paused")
        s = await db_sessions.get_session(conn, "s1")
        assert s["status"] == "paused"
        assert s["ended_at"] is None  # only ended sets ended_at

    @pytest.mark.asyncio
    async def test_update_status_ended_sets_ended_at(self, conn):
        await db_sessions.create_session(conn, session_id="s1")
        await db_sessions.update_session_status(conn, "s1", "ended")
        s = await db_sessions.get_session(conn, "s1")
        assert s["status"] == "ended"
        assert s["ended_at"] is not None

    @pytest.mark.asyncio
    async def test_cas_succeeds_when_current_matches(self, conn):
        """v4·D audit P1 closure: atomic CAS returns True iff
        the row was updated."""
        await db_sessions.create_session(conn, session_id="s1")
        # Status is "active"; CAS active → paused succeeds
        result = await db_sessions.update_session_status_if(
            conn, "s1", "paused", "active",
        )
        assert result is True
        s = await db_sessions.get_session(conn, "s1")
        assert s["status"] == "paused"

    @pytest.mark.asyncio
    async def test_cas_fails_when_current_differs(self, conn):
        """Pin: two concurrent disconnects can't both fire
        session.paused — the second CAS gets False."""
        await db_sessions.create_session(conn, session_id="s1")
        # First CAS succeeds
        first = await db_sessions.update_session_status_if(
            conn, "s1", "paused", "active",
        )
        assert first is True
        # Second CAS fails (status is now "paused", not "active")
        second = await db_sessions.update_session_status_if(
            conn, "s1", "paused", "active",
        )
        assert second is False


# ─── touch_session + increment_message_count ────────────────


class TestTouch:
    @pytest.mark.asyncio
    async def test_touch_updates_last_active_at(self, conn):
        await db_sessions.create_session(conn, session_id="s1")
        before = (await db_sessions.get_session(conn, "s1"))["last_active_at"]
        await asyncio.sleep(0.01)
        await db_sessions.touch_session(conn, "s1")
        after = (await db_sessions.get_session(conn, "s1"))["last_active_at"]
        assert after > before

    @pytest.mark.asyncio
    async def test_increment_count(self, conn):
        await db_sessions.create_session(conn, session_id="s1")
        for _ in range(3):
            await db_sessions.increment_message_count(conn, "s1")
        s = await db_sessions.get_session(conn, "s1")
        assert s["message_count"] == 3


# ─── update_session ─────────────────────────────────────────


class TestUpdateSession:
    @pytest.mark.asyncio
    async def test_update_title(self, conn):
        await db_sessions.create_session(conn, session_id="s1")
        await db_sessions.update_session(conn, "s1", title="My chat")
        s = await db_sessions.get_session(conn, "s1")
        assert s["title"] == "My chat"

    @pytest.mark.asyncio
    async def test_update_metadata_json_encoded(self, conn):
        await db_sessions.create_session(conn, session_id="s1")
        await db_sessions.update_session(
            conn, "s1", metadata={"tags": ["personal"]},
        )
        s = await db_sessions.get_session(conn, "s1")
        assert json.loads(s["metadata"]) == {"tags": ["personal"]}

    @pytest.mark.asyncio
    async def test_update_voice_mode_cast_to_int(self, conn):
        await db_sessions.create_session(conn, session_id="s1")
        await db_sessions.update_session(conn, "s1", voice_mode="3")
        s = await db_sessions.get_session(conn, "s1")
        assert s["voice_mode"] == 3

    @pytest.mark.asyncio
    async def test_update_llm_model_none_becomes_empty_string(self, conn):
        await db_sessions.create_session(conn, session_id="s1")
        await db_sessions.update_session(conn, "s1", llm_model=None)
        s = await db_sessions.get_session(conn, "s1")
        assert s["llm_model"] == ""

    @pytest.mark.asyncio
    async def test_disallowed_field_silently_dropped(self, conn):
        await db_sessions.create_session(conn, session_id="s1")
        # `created_at` not in allowlist
        await db_sessions.update_session(
            conn, "s1", created_at=999999.0,
        )
        s = await db_sessions.get_session(conn, "s1")
        assert s["created_at"] != 999999.0

    @pytest.mark.asyncio
    async def test_no_kwargs_is_silent_noop(self, conn):
        await db_sessions.create_session(conn, session_id="s1")
        # Must NOT raise.
        await db_sessions.update_session(conn, "s1")

    def test_allowlist_constant_pin(self):
        from dragon_voice.db_sessions import _UPDATE_SESSION_ALLOWED_FIELDS
        assert _UPDATE_SESSION_ALLOWED_FIELDS == frozenset({
            "title", "system_prompt", "metadata", "config",
            "voice_mode", "llm_model",
        })


# ─── get_stale_sessions / get_old_paused_sessions ──────────


class TestStaleAndOldPaused:
    @pytest.mark.asyncio
    async def test_stale_returns_active_and_paused_inactive_beyond_timeout(self, conn):
        await db_sessions.create_session(conn, session_id="fresh")
        await db_sessions.create_session(conn, session_id="stale")
        # Manually backdate "stale" to 2 hours ago
        await conn.execute(
            "UPDATE sessions SET last_active_at = ? WHERE id = ?",
            (time.time() - 7200, "stale"),
        )
        await conn.commit()

        result = await db_sessions.get_stale_sessions(
            conn, timeout_seconds=1800,  # 30 min
        )
        ids = {s["id"] for s in result}
        assert "stale" in ids
        assert "fresh" not in ids

    @pytest.mark.asyncio
    async def test_old_paused_returns_only_paused(self, conn):
        await db_sessions.create_session(conn, session_id="active_old")
        await db_sessions.create_session(conn, session_id="paused_old")
        await db_sessions.update_session_status(
            conn, "paused_old", "paused",
        )
        # Backdate both to 60 days ago
        cutoff = time.time() - (60 * 86400)
        await conn.execute(
            "UPDATE sessions SET last_active_at = ?", (cutoff,),
        )
        await conn.commit()

        result = await db_sessions.get_old_paused_sessions(
            conn, retention_days=30,
        )
        ids = {s["id"] for s in result}
        # Only paused — active_old NOT included
        assert "paused_old" in ids
        assert "active_old" not in ids

    @pytest.mark.asyncio
    async def test_old_paused_returns_empty_when_disabled(self, conn):
        """retention_days <= 0 → returns [] (admin opt-out)."""
        result = await db_sessions.get_old_paused_sessions(
            conn, retention_days=0,
        )
        assert result == []
