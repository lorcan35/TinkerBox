"""Tests for ``dragon_voice.db_devices``.

Uses a real in-memory aiosqlite connection (with the prod schema)
so the SQL is exercised end-to-end.  Pin the upsert preserve-
existing-on-empty behaviour, the online-filter, and the update
allowlist.
"""
from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from dragon_voice import db_devices

# Project root → schema.sql lives here
_SCHEMA_PATH = Path(__file__).parent.parent / "schema.sql"


@pytest_asyncio.fixture
async def conn():
    """In-memory SQLite with the prod schema applied."""
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    schema = _SCHEMA_PATH.read_text()
    await db.executescript(schema)
    await db.commit()
    yield db
    await db.close()


# ─── upsert_device ───────────────────────────────────────────


class TestUpsert:
    @pytest.mark.asyncio
    async def test_first_register_creates_row(self, conn):
        device = await db_devices.upsert_device(
            conn,
            device_id="tab5-7",
            hardware_id="hw-abc",
            name="Living room",
            firmware_ver="1.2.3",
            platform="esp32-p4",
            capabilities={"widgets": {"types": ["live"]}},
        )
        assert device["id"] == "tab5-7"
        assert device["name"] == "Living room"
        assert device["firmware_ver"] == "1.2.3"
        assert device["is_online"] == 1

    @pytest.mark.asyncio
    async def test_re_register_with_empty_name_preserves_existing(self, conn):
        """Audit invariant: re-register frame from Tab5 firmware
        that omits `name` MUST NOT blank the user-set name."""
        await db_devices.upsert_device(
            conn,
            device_id="tab5-7", hardware_id="hw",
            name="My Tab",
        )
        await db_devices.upsert_device(
            conn,
            device_id="tab5-7", hardware_id="hw",
            name="",  # empty re-register
        )
        device = await db_devices.get_device(conn, "tab5-7")
        assert device["name"] == "My Tab"  # preserved

    @pytest.mark.asyncio
    async def test_re_register_with_empty_capabilities_preserves(self, conn):
        await db_devices.upsert_device(
            conn,
            device_id="tab5-7", hardware_id="hw",
            capabilities={"widgets": {"types": ["live", "card"]}},
        )
        # Re-register with empty caps
        await db_devices.upsert_device(
            conn,
            device_id="tab5-7", hardware_id="hw",
            capabilities=None,
        )
        device = await db_devices.get_device(conn, "tab5-7")
        caps = json.loads(device["capabilities"])
        assert caps == {"widgets": {"types": ["live", "card"]}}

    @pytest.mark.asyncio
    async def test_re_register_with_new_name_overwrites(self, conn):
        await db_devices.upsert_device(
            conn, device_id="tab5-7", hardware_id="hw", name="Old name",
        )
        await db_devices.upsert_device(
            conn, device_id="tab5-7", hardware_id="hw", name="New name",
        )
        device = await db_devices.get_device(conn, "tab5-7")
        assert device["name"] == "New name"

    @pytest.mark.asyncio
    async def test_re_register_marks_online_again(self, conn):
        await db_devices.upsert_device(
            conn, device_id="x", hardware_id="hw",
        )
        await db_devices.set_device_online(conn, "x", False)
        # Re-register should flip back to online
        await db_devices.upsert_device(
            conn, device_id="x", hardware_id="hw",
        )
        device = await db_devices.get_device(conn, "x")
        assert device["is_online"] == 1


# ─── get_device ──────────────────────────────────────────────


class TestGet:
    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self, conn):
        assert await db_devices.get_device(conn, "nope") is None


# ─── list_devices ────────────────────────────────────────────


class TestList:
    @pytest.mark.asyncio
    async def test_lists_all_when_online_only_false(self, conn):
        # Schema enforces UNIQUE(hardware_id) — distinct per device.
        await db_devices.upsert_device(conn, device_id="a", hardware_id="hw-a")
        await db_devices.upsert_device(conn, device_id="b", hardware_id="hw-b")
        await db_devices.set_device_online(conn, "b", False)

        devices = await db_devices.list_devices(conn, online_only=False)
        ids = {d["id"] for d in devices}
        assert ids == {"a", "b"}

    @pytest.mark.asyncio
    async def test_online_only_filters_offline(self, conn):
        await db_devices.upsert_device(conn, device_id="a", hardware_id="hw-a")
        await db_devices.upsert_device(conn, device_id="b", hardware_id="hw-b")
        await db_devices.set_device_online(conn, "b", False)

        devices = await db_devices.list_devices(conn, online_only=True)
        ids = {d["id"] for d in devices}
        assert ids == {"a"}

    @pytest.mark.asyncio
    async def test_ordered_by_last_seen_desc(self, conn):
        import asyncio
        await db_devices.upsert_device(
            conn, device_id="first", hardware_id="hw-1",
        )
        await asyncio.sleep(0.01)
        await db_devices.upsert_device(
            conn, device_id="second", hardware_id="hw-2",
        )

        devices = await db_devices.list_devices(conn)
        # Most-recently-seen first
        assert devices[0]["id"] == "second"
        assert devices[1]["id"] == "first"


# ─── set_device_online ───────────────────────────────────────


class TestSetOnline:
    @pytest.mark.asyncio
    async def test_toggle_online_off(self, conn):
        await db_devices.upsert_device(conn, device_id="x", hardware_id="hw")
        await db_devices.set_device_online(conn, "x", False)
        device = await db_devices.get_device(conn, "x")
        assert device["is_online"] == 0

    @pytest.mark.asyncio
    async def test_toggle_online_on(self, conn):
        await db_devices.upsert_device(conn, device_id="x", hardware_id="hw")
        await db_devices.set_device_online(conn, "x", False)
        await db_devices.set_device_online(conn, "x", True)
        device = await db_devices.get_device(conn, "x")
        assert device["is_online"] == 1


# ─── update_device ───────────────────────────────────────────


class TestUpdate:
    @pytest.mark.asyncio
    async def test_update_name(self, conn):
        await db_devices.upsert_device(
            conn, device_id="x", hardware_id="hw", name="Old",
        )
        await db_devices.update_device(conn, "x", name="New")
        device = await db_devices.get_device(conn, "x")
        assert device["name"] == "New"

    @pytest.mark.asyncio
    async def test_update_config_json_encoded(self, conn):
        await db_devices.upsert_device(conn, device_id="x", hardware_id="hw")
        await db_devices.update_device(
            conn, "x", config={"key": "value", "n": 42},
        )
        device = await db_devices.get_device(conn, "x")
        cfg = json.loads(device["config"])
        assert cfg == {"key": "value", "n": 42}

    @pytest.mark.asyncio
    async def test_disallowed_field_silently_dropped(self, conn):
        """Pin: only the allowlisted fields go through.
        Pre-extract this was a hardcoded `{"name", "config"}`
        set; future allowlist edits happen in one place
        (`_UPDATE_DEVICE_ALLOWED_FIELDS`)."""
        await db_devices.upsert_device(
            conn, device_id="x", hardware_id="hw", platform="orig",
        )
        await db_devices.update_device(
            conn, "x", platform="hacked",  # not allowed
        )
        device = await db_devices.get_device(conn, "x")
        assert device["platform"] == "orig"  # unchanged

    @pytest.mark.asyncio
    async def test_no_kwargs_is_silent_noop(self, conn):
        await db_devices.upsert_device(conn, device_id="x", hardware_id="hw")
        # Must NOT raise.
        await db_devices.update_device(conn, "x")

    @pytest.mark.asyncio
    async def test_allowlist_constant_pin(self):
        """Pin the allowlist so a future widening (e.g. firmware_ver
        added externally) is a deliberate edit."""
        from dragon_voice.db_devices import _UPDATE_DEVICE_ALLOWED_FIELDS
        assert _UPDATE_DEVICE_ALLOWED_FIELDS == frozenset({"name", "config"})


# ─── delete_device ───────────────────────────────────────────


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_removes_row(self, conn):
        await db_devices.upsert_device(conn, device_id="x", hardware_id="hw")
        await db_devices.delete_device(conn, "x")
        assert await db_devices.get_device(conn, "x") is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_is_silent(self, conn):
        # Must NOT raise.
        await db_devices.delete_device(conn, "never-existed")
