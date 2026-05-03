"""Tests for ``dragon_voice.db_notes``, ``db_events``, ``db_config``.

End-to-end SQL tests against an in-memory aiosqlite connection
with the prod schema.  Covers the three smaller SRP-7
mixins in one file (40 + 50 + 75 LOC of source → ~30 tests).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from dragon_voice import db_config, db_events, db_notes, db_sessions

_SCHEMA_PATH = Path(__file__).parent.parent / "schema.sql"


@pytest_asyncio.fixture
async def conn():
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.executescript(_SCHEMA_PATH.read_text())
    await db.commit()
    yield db
    await db.close()


# ─── db_notes ───────────────────────────────────────────────


class TestNotes:
    @pytest.mark.asyncio
    async def test_add_minimal_required_fields(self, conn):
        note = await db_notes.add_note(conn, note_id="n1")
        assert note["id"] == "n1"
        assert note["title"] == ""
        assert note["transcript"] == ""

    @pytest.mark.asyncio
    async def test_add_full_payload(self, conn):
        await db_sessions.create_session(conn, session_id="s1")
        note = await db_notes.add_note(
            conn, note_id="n1", session_id="s1",
            title="Meeting recap",
            transcript="long transcript...",
            summary="short summary",
            tags=["work", "ai"],
            source="audio",  # schema CHECK: audio|text|import only
            duration_s=125.5,
            word_count=234,
        )
        assert note["title"] == "Meeting recap"
        assert note["source"] == "audio"
        assert note["duration_s"] == 125.5
        assert note["word_count"] == 234
        assert json.loads(note["tags"]) == ["work", "ai"]

    @pytest.mark.asyncio
    async def test_get_returns_none_when_missing(self, conn):
        assert await db_notes.get_note(conn, "nope") is None

    @pytest.mark.asyncio
    async def test_list_filter_by_session(self, conn):
        await db_sessions.create_session(conn, session_id="a")
        await db_sessions.create_session(conn, session_id="b")
        await db_notes.add_note(conn, note_id="n1", session_id="a")
        await db_notes.add_note(conn, note_id="n2", session_id="b")

        a_notes = await db_notes.list_notes(conn, session_id="a")
        assert {n["id"] for n in a_notes} == {"n1"}

    @pytest.mark.asyncio
    async def test_list_ordered_newest_first(self, conn):
        await db_notes.add_note(conn, note_id="n1")
        await asyncio.sleep(0.01)
        await db_notes.add_note(conn, note_id="n2")
        notes = await db_notes.list_notes(conn)
        # Newest first
        assert notes[0]["id"] == "n2"


# ─── db_events ──────────────────────────────────────────────


class TestEvents:
    @pytest.mark.asyncio
    async def test_add_returns_auto_id(self, conn):
        eid1 = await db_events.add_event(conn, "boot")
        eid2 = await db_events.add_event(conn, "boot")
        assert eid2 > eid1  # auto-incremented

    @pytest.mark.asyncio
    async def test_add_with_data_json_encoded(self, conn):
        eid = await db_events.add_event(
            conn, "api_usage",
            data={"model": "claude-haiku", "tokens": 100},
        )
        events = await db_events.get_events(conn, since_id=eid - 1)
        assert json.loads(events[0]["data"]) == {
            "model": "claude-haiku", "tokens": 100,
        }

    @pytest.mark.asyncio
    async def test_get_filter_by_type(self, conn):
        await db_events.add_event(conn, "boot")
        await db_events.add_event(conn, "shutdown")
        await db_events.add_event(conn, "boot")

        boots = await db_events.get_events(conn, event_type="boot")
        assert len(boots) == 2
        assert all(e["type"] == "boot" for e in boots)

    @pytest.mark.asyncio
    async def test_get_filter_by_session(self, conn):
        await db_sessions.create_session(conn, session_id="s1")
        await db_events.add_event(conn, "x", session_id="s1")
        await db_events.add_event(conn, "x")  # no session

        s1_events = await db_events.get_events(conn, session_id="s1")
        assert len(s1_events) == 1

    @pytest.mark.asyncio
    async def test_since_id_polling_pin(self, conn):
        """Pin: get_events(since_id=N) returns events with id > N
        — supports polling clients that track the last-seen id."""
        eid1 = await db_events.add_event(conn, "a")
        eid2 = await db_events.add_event(conn, "b")
        eid3 = await db_events.add_event(conn, "c")

        result = await db_events.get_events(conn, since_id=eid1)
        ids = [e["id"] for e in result]
        assert eid1 not in ids
        assert eid2 in ids
        assert eid3 in ids

    @pytest.mark.asyncio
    async def test_ordered_ascending_by_id(self, conn):
        """Polling clients use the LAST returned id as the next
        since_id — order MUST be ASC so they don't miss events."""
        await db_events.add_event(conn, "first")
        await db_events.add_event(conn, "second")
        await db_events.add_event(conn, "third")

        result = await db_events.get_events(conn)
        types = [e["type"] for e in result]
        assert types == ["first", "second", "third"]


# ─── db_config ──────────────────────────────────────────────


class TestConfig:
    @pytest.mark.asyncio
    async def test_set_and_get_global(self, conn):
        await db_config.set_config(conn, "key", '"value"')
        assert await db_config.get_config(conn, "key") == '"value"'

    @pytest.mark.asyncio
    async def test_get_returns_none_when_missing(self, conn):
        assert await db_config.get_config(conn, "missing") is None

    @pytest.mark.asyncio
    async def test_set_upserts_at_scoped_key(self, conn):
        """Pin upsert behaviour at a non-NULL scope_id (SQLite
        treats NULL columns as distinct in PRIMARY KEY, so the
        upsert at global/scope_id=NULL doesn't collapse two
        inserts — that's a pre-existing schema quirk preserved
        verbatim from the pre-extract code)."""
        await db_sessions.create_session(conn, session_id="s1")
        await db_config.set_config(
            conn, "key", '"first"',
            scope="session", scope_id="s1",
        )
        await db_config.set_config(
            conn, "key", '"second"',
            scope="session", scope_id="s1",
        )
        assert await db_config.get_config(
            conn, "key", scope="session", scope_id="s1",
        ) == '"second"'

    @pytest.mark.asyncio
    async def test_scoped_config_independent(self, conn):
        await db_sessions.create_session(conn, session_id="s1")
        await db_config.set_config(
            conn, "voice_mode", "0", scope="global",
        )
        await db_config.set_config(
            conn, "voice_mode", "2", scope="session", scope_id="s1",
        )

        glob = await db_config.get_config(conn, "voice_mode", scope="global")
        sess = await db_config.get_config(
            conn, "voice_mode", scope="session", scope_id="s1",
        )
        assert glob == "0"
        assert sess == "2"

    @pytest.mark.asyncio
    async def test_resolve_session_overrides_global(self, conn):
        """Pin: scope-resolution priority is session > device > global."""
        await db_sessions.create_session(conn, session_id="s1")
        await db_config.set_config(conn, "k", "global", scope="global")
        await db_config.set_config(
            conn, "k", "session", scope="session", scope_id="s1",
        )

        resolved = await db_config.get_resolved_config(
            conn, "k", session_id="s1",
        )
        assert resolved == "session"

    @pytest.mark.asyncio
    async def test_resolve_device_overrides_global_when_no_session(self, conn):
        from dragon_voice import db_devices
        await db_devices.upsert_device(conn, device_id="d1", hardware_id="h1")
        await db_config.set_config(conn, "k", "global", scope="global")
        await db_config.set_config(
            conn, "k", "device", scope="device", scope_id="d1",
        )

        resolved = await db_config.get_resolved_config(
            conn, "k", device_id="d1",
        )
        assert resolved == "device"

    @pytest.mark.asyncio
    async def test_resolve_falls_back_to_global(self, conn):
        from dragon_voice import db_devices
        await db_sessions.create_session(conn, session_id="s1")
        await db_devices.upsert_device(conn, device_id="d1", hardware_id="h1")
        await db_config.set_config(conn, "k", "GLOBAL_VAL")

        resolved = await db_config.get_resolved_config(
            conn, "k", device_id="d1", session_id="s1",
        )
        assert resolved == "GLOBAL_VAL"

    @pytest.mark.asyncio
    async def test_list_returns_dict(self, conn):
        await db_config.set_config(conn, "a", "1")
        await db_config.set_config(conn, "b", "2")

        result = await db_config.list_config(conn)
        assert result == {"a": "1", "b": "2"}

    @pytest.mark.asyncio
    async def test_delete_returns_true_when_existed(self, conn):
        await db_config.set_config(conn, "k", "v")
        assert await db_config.delete_config(conn, "k") is True
        assert await db_config.get_config(conn, "k") is None

    @pytest.mark.asyncio
    async def test_delete_returns_false_when_missing(self, conn):
        assert await db_config.delete_config(conn, "never-existed") is False
