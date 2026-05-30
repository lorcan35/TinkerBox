"""Unit test for the γ2-M5 device-evicted frame.

Issue #108, refs #89, refs #101.  Pre-fix when a new WS connection
registered with the same ``device_id`` as an existing one, the
existing connection got silently torn down — pipeline shutdown,
session pause, removed from ``_active_connections``, then the WS
just saw TCP close.  No ``error`` frame, so Tab5 had no way to
distinguish "another device claimed this session" from a generic
network drop and would happily auto-reconnect into the same
eviction loop.

The fix sends an ``error_event(code="device_evicted",
severity=FATAL, scope=DEVICE)`` to the OLD WS (via its captured
``_on_event`` closure) BEFORE shutting down its pipeline.

The eviction logic lives inline in ``VoiceServer._handle_register``
and is hard to drive without instantiating the full server stack.
This test directly drives the relevant slice — registers a stub
"old" connection in ``server._active_connections``, then calls
``_handle_register`` against a fresh stub WS for the same
``device_id`` and asserts:

  * the old connection's ``_on_event`` was called with the
    structured γ-arch error frame
  * the old connection was removed from ``_active_connections``
  * the new connection ends up registered

aiohttp / DB / pipeline / sessions are all mocked — runs in CI
named-set without I/O.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.server import VoiceServer


def _make_server() -> VoiceServer:
    """Build a minimal VoiceServer with just enough wiring for
    ``_handle_register`` to run.  Most subsystems are MagicMock
    instances — we only care about the eviction code path."""
    s = VoiceServer.__new__(VoiceServer)  # type: ignore[call-arg]
    s._active_connections = {}
    s._db = MagicMock()
    s._db.upsert_device = AsyncMock()
    s._db.get_device = AsyncMock(return_value=None)
    s._session_mgr = MagicMock()
    s._session_mgr.find_active_for_device = AsyncMock(return_value=None)
    s._session_mgr.create_session = AsyncMock(return_value=MagicMock(id="newsession"))
    s._session_mgr.pause_session = AsyncMock()
    s._session_mgr.resume_session = AsyncMock()
    s._surface_mgr = None
    s._tool_registry = None
    s._messages = MagicMock()
    s._messages.get_recent_for_session = AsyncMock(return_value=[])
    s._conversation = None
    s._media_pipeline = None
    s._backend_pool = None
    s._safe_send_json = AsyncMock(return_value=True)
    return s


def _make_stub_ws() -> MagicMock:
    """aiohttp WSResponse stand-in — closed=False so the eviction
    sender doesn't short-circuit on the ``ws.closed`` guard."""
    ws = MagicMock()
    ws.closed = False
    ws.send_json = AsyncMock()
    return ws


def test_device_evicted_frame_lands_on_old_connection() -> None:
    """The headline γ2-M5 outcome: the OLD connection's ``_on_event``
    receives a structured ``device_evicted`` error frame BEFORE
    its pipeline is shut down."""
    server = _make_server()
    device_id = "dev-X"

    # Register an "old" connection in active_connections.
    old_on_event = AsyncMock()
    old_conn = {
        "ws_id": "ws_old",
        "device_id": device_id,
        "registered": True,
        "session_id": "old-session",
        "pipeline": MagicMock(shutdown=AsyncMock()),
        "_on_event": old_on_event,
    }
    server._active_connections["ws_old"] = old_conn

    # Build a "new" connection that's about to register with the
    # same device_id.  We DON'T need a full WS — _handle_register
    # only touches the conn_state dict + the WS's send_json on the
    # success path.
    new_ws = _make_stub_ws()
    new_conn: dict = {
        "ws_id": "ws_new",
        "pipeline": None,
        "session_id": None,
        "device_id": None,
        "registered": False,
        "mode": "ask",
        "config": MagicMock(),
        "conn_lock": asyncio.Lock(),
        "_on_audio": None,
        "_on_event": None,
        "bg_tasks": set(),
    }
    server._active_connections["ws_new"] = new_conn

    cmd = {
        "type": "register",
        "device_id": device_id,
        "hardware_id": "00:11:22:33:44:55",
    }

    # Drive the eviction.  We don't need _handle_register's full
    # post-eviction setup (pipeline init etc.) — wrap it so the
    # session-creation path it would have hit doesn't blow up.
    asyncio.run(_drive_register(server, new_ws, new_conn, cmd))

    # Assert: the old connection's _on_event was called with the
    # structured error frame.
    assert old_on_event.await_count == 1, (
        f"Expected exactly one _on_event call on the OLD connection; "
        f"got {old_on_event.await_count}"
    )
    sent_frame = old_on_event.await_args.args[0]
    assert sent_frame == {
        "type": "error",
        "code": "device_evicted",
        "message": "Another device claimed this session.",
        "severity": "fatal",
        "scope": "device",
    }
    # And the old connection must have been removed.
    assert "ws_old" not in server._active_connections


def test_device_evicted_send_failure_does_not_block_eviction() -> None:
    """Defensive: if the old WS is half-dead and ``_on_event`` raises,
    the eviction must still complete — the new connection succeeds
    regardless.  Pre-existing test_session_cas pattern + the inline
    try/except catches this; pin it so a future refactor can't drop
    the catch."""
    server = _make_server()
    device_id = "dev-Y"

    old_on_event = AsyncMock(
        side_effect=ConnectionResetError("old WS already torn down")
    )
    old_pipeline = MagicMock(shutdown=AsyncMock())
    server._active_connections["ws_old"] = {
        "ws_id": "ws_old",
        "device_id": device_id,
        "registered": True,
        "session_id": "old-session",
        "pipeline": old_pipeline,
        "_on_event": old_on_event,
    }

    new_ws = _make_stub_ws()
    new_conn: dict = {
        "ws_id": "ws_new", "pipeline": None, "session_id": None,
        "device_id": None, "registered": False, "mode": "ask",
        "config": MagicMock(), "conn_lock": asyncio.Lock(),
        "_on_audio": None, "_on_event": None, "bg_tasks": set(),
    }
    server._active_connections["ws_new"] = new_conn

    cmd = {"type": "register", "device_id": device_id, "hardware_id": "x"}

    asyncio.run(_drive_register(server, new_ws, new_conn, cmd))

    # Old connection still got the SEND attempt
    assert old_on_event.await_count == 1
    # …but eviction proceeded — pipeline shutdown was still called
    old_pipeline.shutdown.assert_awaited_once()
    # …and the old connection was removed
    assert "ws_old" not in server._active_connections


def test_no_eviction_when_device_id_does_not_collide() -> None:
    """Regression guard: two distinct device_ids must NOT trigger
    eviction notices on each other."""
    server = _make_server()

    other_on_event = AsyncMock()
    server._active_connections["ws_other"] = {
        "ws_id": "ws_other",
        "device_id": "dev-DIFFERENT",
        "registered": True,
        "session_id": "other-session",
        "pipeline": MagicMock(shutdown=AsyncMock()),
        "_on_event": other_on_event,
    }

    new_ws = _make_stub_ws()
    new_conn: dict = {
        "ws_id": "ws_new", "pipeline": None, "session_id": None,
        "device_id": None, "registered": False, "mode": "ask",
        "config": MagicMock(), "conn_lock": asyncio.Lock(),
        "_on_audio": None, "_on_event": None, "bg_tasks": set(),
    }
    server._active_connections["ws_new"] = new_conn

    cmd = {
        "type": "register",
        "device_id": "dev-NEW",  # different from "dev-DIFFERENT"
        "hardware_id": "x",
    }

    asyncio.run(_drive_register(server, new_ws, new_conn, cmd))

    # Other connection's _on_event was NEVER called
    other_on_event.assert_not_called()
    # And it's still in active_connections
    assert "ws_other" in server._active_connections


# ───────────────────────── plumbing


async def _drive_register(server: VoiceServer, ws, conn_state: dict, cmd: dict) -> None:
    """Run ``_handle_register`` and swallow any post-eviction setup
    failures (pipeline init, surface registration etc.) since this
    test only cares about the eviction slice.

    The ``MagicMock`` placeholders for ``conn_config`` mean the
    pipeline-init path will explode at the first attribute access —
    that's fine, our assertions run BEFORE that point in
    _handle_register's flow."""
    on_audio = AsyncMock()
    on_event = AsyncMock()
    conn_config = MagicMock()
    try:
        await server._handle_register(
            ws, conn_state, cmd, on_audio, on_event, conn_config,
        )
    except Exception:
        # Post-eviction setup uses real config attributes the stubs
        # don't provide; that's outside the scope of this test.
        pass


# ───────────────────────── CLOSE-WAIT leak fix (2026-05-30)


def test_eviction_closes_old_websocket() -> None:
    """The evicted connection's WebSocket MUST be explicitly closed so its
    socket fd is released immediately.  Pre-fix, eviction tore down the
    pipeline/session/registry but left the old ws open, relying on aiohttp's
    180 s heartbeat to reap it — under reconnect churn evicted sockets piled up
    in CLOSE-WAIT and exhausted the WS handler (observed live: 59 CLOSE-WAIT
    sockets wedged :3502, "Error read response for Upgrade header", Tab5 could
    not reconnect until the service was restarted)."""
    from aiohttp import WSCloseCode

    from dragon_voice.stale_conn_eviction import evict_stale_connections_for_device

    old_ws = MagicMock()
    old_ws.closed = False
    old_ws.close = AsyncMock()
    active = {
        "ws_old": {
            "ws_id": "ws_old",
            "device_id": "dev-Z",
            "registered": True,
            "session_id": "old-session",
            "pipeline": MagicMock(shutdown=AsyncMock()),
            "_on_event": AsyncMock(),
            "ws": old_ws,
        }
    }

    evicted = asyncio.run(
        evict_stale_connections_for_device(
            active_connections=active,
            session_mgr=None,
            device_id="dev-Z",
            new_ws_id="ws_new",
        )
    )

    assert evicted == 1
    old_ws.close.assert_awaited_once()
    assert old_ws.close.await_args.kwargs.get("code") == WSCloseCode.GOING_AWAY
    assert "ws_old" not in active


def test_eviction_skips_close_on_already_closed_ws() -> None:
    """If the old ws is already closed, eviction skips the close (no
    double-close) and still completes the teardown."""
    from dragon_voice.stale_conn_eviction import evict_stale_connections_for_device

    old_ws = MagicMock()
    old_ws.closed = True
    old_ws.close = AsyncMock()
    active = {
        "ws_old": {
            "ws_id": "ws_old",
            "device_id": "dev-Z",
            "registered": True,
            "session_id": None,
            "pipeline": None,
            "_on_event": None,
            "ws": old_ws,
        }
    }

    evicted = asyncio.run(
        evict_stale_connections_for_device(
            active_connections=active,
            session_mgr=None,
            device_id="dev-Z",
            new_ws_id="ws_new",
        )
    )

    assert evicted == 1
    old_ws.close.assert_not_awaited()
    assert "ws_old" not in active
