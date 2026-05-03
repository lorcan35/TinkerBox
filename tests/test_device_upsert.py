"""Tests for ``dragon_voice.device_upsert.upsert_device_with_collision_guard``.

Pin five branches of the D2 audit fix:

  1. Happy path — db.upsert_device called with right args, returns True.
  2. hardware_id collision → False, γ-arch FATAL/DEVICE error sent.
  3. Non-collision IntegrityError → re-raised (real bug, not user-facing).
  4. WS closed during collision → returns False without trying to send.
  5. Generic exception types (not IntegrityError) → still raise (the
     guard is specifically for hardware_id collision, not all errors).
"""
from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.device_upsert import upsert_device_with_collision_guard


def _make_ws(*, closed: bool = False) -> MagicMock:
    ws = MagicMock()
    ws.closed = closed
    return ws


def _make_db(*, raises: Exception | None = None) -> MagicMock:
    db = MagicMock()
    if raises is not None:
        db.upsert_device = AsyncMock(side_effect=raises)
    else:
        db.upsert_device = AsyncMock()
    return db


def _make_safe_send_json() -> AsyncMock:
    return AsyncMock(return_value=True)


@pytest.mark.asyncio
async def test_happy_path_returns_true_with_correct_kwargs():
    ws = _make_ws()
    db = _make_db()
    send = _make_safe_send_json()

    out = await upsert_device_with_collision_guard(
        ws,
        db=db,
        device_id="dev-A",
        hardware_id="00:11:22:33:44:55",
        name="Tab5 Living Room",
        firmware_ver="0.7.1",
        platform="esp32p4",
        capabilities={"audio_codec": ["pcm", "opus"]},
        safe_send_json=send,
    )

    assert out is True
    db.upsert_device.assert_awaited_once_with(
        device_id="dev-A",
        hardware_id="00:11:22:33:44:55",
        name="Tab5 Living Room",
        firmware_ver="0.7.1",
        platform="esp32p4",
        capabilities={"audio_codec": ["pcm", "opus"]},
    )
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_hardware_id_collision_sends_gamma_error_and_returns_false():
    """D2 audit fix anchor: collision triggers a structured
    error_event with the canonical code 'hardware_id_collision'."""
    ws = _make_ws()
    db = _make_db(
        raises=sqlite3.IntegrityError("UNIQUE constraint failed: devices.hardware_id"),
    )
    send = _make_safe_send_json()

    out = await upsert_device_with_collision_guard(
        ws,
        db=db,
        device_id="dev-DUP",
        hardware_id="00:11:22:33:44:55",
        safe_send_json=send,
    )

    assert out is False
    send.assert_awaited_once()
    payload = send.await_args.args[1]
    assert payload.get("type") == "error"
    assert payload.get("code") == "hardware_id_collision"
    assert payload.get("severity") == "fatal"
    assert payload.get("scope") == "device"


@pytest.mark.asyncio
async def test_non_collision_integrity_error_is_re_raised():
    """An IntegrityError that's NOT about hardware_id is a real
    bug; let it propagate to the outer handler instead of
    silently swallowing as a collision."""
    ws = _make_ws()
    db = _make_db(
        raises=sqlite3.IntegrityError("UNIQUE constraint failed: devices.device_id"),
    )

    with pytest.raises(sqlite3.IntegrityError):
        await upsert_device_with_collision_guard(
            ws,
            db=db,
            device_id="dev-A",
            hardware_id="00:11:22:33:44:55",
            safe_send_json=_make_safe_send_json(),
        )


@pytest.mark.asyncio
async def test_ws_closed_during_collision_returns_false_no_send():
    """If the WS is already closed when the collision fires, we
    return False but don't try to send (which would log spurious
    transport errors)."""
    ws = _make_ws(closed=True)
    db = _make_db(
        raises=sqlite3.IntegrityError("UNIQUE constraint failed: devices.hardware_id"),
    )
    send = _make_safe_send_json()

    out = await upsert_device_with_collision_guard(
        ws,
        db=db,
        device_id="dev-DUP",
        hardware_id="00:11:22:33:44:55",
        safe_send_json=send,
    )

    assert out is False
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_integrity_exception_propagates():
    """The guard is specifically for IntegrityError — generic
    exceptions (e.g. operational/connection issues) should
    propagate to the outer handler."""
    ws = _make_ws()
    db = _make_db(raises=RuntimeError("DB connection lost"))

    with pytest.raises(RuntimeError, match="DB connection lost"):
        await upsert_device_with_collision_guard(
            ws,
            db=db,
            device_id="dev-A",
            hardware_id="00:11:22:33:44:55",
            safe_send_json=_make_safe_send_json(),
        )


@pytest.mark.asyncio
async def test_collision_message_case_insensitive():
    """The collision detection lowercases the error string before
    matching 'hardware_id' — pin the case-insensitive guard so a
    future SQLite version emitting upper-case messages still gets
    caught."""
    ws = _make_ws()
    db = _make_db(
        raises=sqlite3.IntegrityError("UNIQUE constraint failed: HARDWARE_ID"),
    )
    send = _make_safe_send_json()

    out = await upsert_device_with_collision_guard(
        ws,
        db=db,
        device_id="dev-DUP",
        hardware_id="00:11:22:33:44:55",
        safe_send_json=send,
    )
    assert out is False
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_default_kwargs_omit_optional_fields_as_empty():
    """Calling without name/firmware_ver/platform should pass
    empty strings to the DB (not None or omit) — match
    pre-extract behaviour at server.py:1062-1065."""
    ws = _make_ws()
    db = _make_db()

    await upsert_device_with_collision_guard(
        ws,
        db=db,
        device_id="dev-A",
        hardware_id="00:11:22:33:44:55",
        safe_send_json=_make_safe_send_json(),
    )

    kwargs = db.upsert_device.await_args.kwargs
    assert kwargs["name"] == ""
    assert kwargs["firmware_ver"] == ""
    assert kwargs["platform"] == ""
    assert kwargs["capabilities"] is None
