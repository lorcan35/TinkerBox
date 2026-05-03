"""Tests for ``dragon_voice.stale_conn_eviction.evict_stale_connections_for_device``.

Pin every branch of the P13 audit fix:

  1. No stale match → 0 evicted, no mutation.
  2. Self-match (new_ws_id) → skipped.
  3. Different device → skipped.
  4. Same device but registered=False → skipped (already torn down).
  5. Happy path → device_evicted error sent, pipeline shutdown,
     session paused, conn removed, returns 1.
  6. _on_event raises → eviction still proceeds (log+swallow).
  7. pipeline.shutdown raises → eviction still proceeds.
  8. session_mgr is None → no pause attempt, eviction still proceeds.
  9. Multiple stale entries for same device → all evicted, count returned.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.stale_conn_eviction import evict_stale_connections_for_device


def _make_old_conn(
    *,
    device_id: str = "dev-A",
    session_id: str = "sess-A",
    registered: bool = True,
    has_pipeline: bool = True,
    has_on_event: bool = True,
) -> dict:
    """Build an old conn_state dict that matches the historical
    inline shape of `_handle_register`'s state.  Plain dict for
    test-mock compat (ConnState's dict-shim works either way)."""
    conn: dict = {
        "device_id": device_id,
        "session_id": session_id,
        "registered": registered,
        "pipeline": None,
    }
    if has_pipeline:
        p = MagicMock()
        p.shutdown = AsyncMock()
        conn["pipeline"] = p
    if has_on_event:
        conn["_on_event"] = AsyncMock()
    return conn


def _make_session_mgr() -> MagicMock:
    sm = MagicMock()
    sm.pause_session = AsyncMock()
    return sm


# ─── No-eviction branches ────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_active_conns_returns_zero():
    out = await evict_stale_connections_for_device(
        active_connections={},
        session_mgr=_make_session_mgr(),
        device_id="dev-A",
        new_ws_id="ws-new",
    )
    assert out == 0


@pytest.mark.asyncio
async def test_self_match_skipped():
    """The new connection is in active_connections by ws_id; we
    must NOT evict ourselves."""
    self_conn = _make_old_conn(device_id="dev-A")
    conns = {"ws-new": self_conn}

    out = await evict_stale_connections_for_device(
        active_connections=conns,
        session_mgr=_make_session_mgr(),
        device_id="dev-A",
        new_ws_id="ws-new",
    )
    assert out == 0
    # self_conn still present
    assert "ws-new" in conns
    self_conn["_on_event"].assert_not_called()


@pytest.mark.asyncio
async def test_different_device_skipped():
    other = _make_old_conn(device_id="dev-OTHER")
    conns = {"ws-old": other}

    out = await evict_stale_connections_for_device(
        active_connections=conns,
        session_mgr=_make_session_mgr(),
        device_id="dev-A",
        new_ws_id="ws-new",
    )
    assert out == 0
    assert "ws-old" in conns
    other["_on_event"].assert_not_called()


@pytest.mark.asyncio
async def test_unregistered_old_conn_skipped():
    """An old conn that's not registered is already torn down;
    skip it.  Pre-fix we used to evict these too which was a
    spurious extra round of pause_session calls."""
    pending = _make_old_conn(device_id="dev-A", registered=False)
    conns = {"ws-pending": pending}

    out = await evict_stale_connections_for_device(
        active_connections=conns,
        session_mgr=_make_session_mgr(),
        device_id="dev-A",
        new_ws_id="ws-new",
    )
    assert out == 0
    assert "ws-pending" in conns


# ─── Happy path ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_evicts_and_returns_one():
    old = _make_old_conn(device_id="dev-A", session_id="sess-A")
    sm = _make_session_mgr()
    conns = {"ws-old": old, "ws-other": _make_old_conn(device_id="dev-OTHER")}

    out = await evict_stale_connections_for_device(
        active_connections=conns,
        session_mgr=sm,
        device_id="dev-A",
        new_ws_id="ws-new",
    )

    assert out == 1
    # The device_evicted error frame was sent to the OLD on_event
    old["_on_event"].assert_awaited_once()
    sent_event = old["_on_event"].await_args.args[0]
    assert sent_event.get("type") == "error"
    assert sent_event.get("code") == "device_evicted"
    # Pipeline shut down + cleared
    assert old["pipeline"] is None  # cleared after shutdown
    # Session paused
    sm.pause_session.assert_awaited_once_with("sess-A")
    # Marked unregistered
    assert old["registered"] is False
    # Removed from active_connections
    assert "ws-old" not in conns
    # Other-device entry untouched
    assert "ws-other" in conns


# ─── Failure-isolation branches ──────────────────────────────────


@pytest.mark.asyncio
async def test_on_event_failure_does_not_block_eviction():
    """If the old client's transport is already half-closed, the
    notify can fail.  Eviction MUST still complete — the new
    connection's success matters more."""
    old = _make_old_conn(device_id="dev-A")
    old["_on_event"] = AsyncMock(side_effect=ConnectionResetError("dead"))
    sm = _make_session_mgr()
    conns = {"ws-old": old}

    out = await evict_stale_connections_for_device(
        active_connections=conns,
        session_mgr=sm,
        device_id="dev-A",
        new_ws_id="ws-new",
    )

    assert out == 1
    # Pipeline still shut down + session still paused despite the
    # notify failure.
    sm.pause_session.assert_awaited_once_with("sess-A")
    assert "ws-old" not in conns


@pytest.mark.asyncio
async def test_pipeline_shutdown_failure_does_not_block_eviction():
    old = _make_old_conn(device_id="dev-A")
    old["pipeline"].shutdown = AsyncMock(side_effect=RuntimeError("shutdown blew up"))
    sm = _make_session_mgr()
    conns = {"ws-old": old}

    out = await evict_stale_connections_for_device(
        active_connections=conns,
        session_mgr=sm,
        device_id="dev-A",
        new_ws_id="ws-new",
    )

    assert out == 1
    # Session still paused, conn still removed.
    sm.pause_session.assert_awaited_once()
    assert "ws-old" not in conns


@pytest.mark.asyncio
async def test_session_mgr_none_skips_pause_but_evicts():
    """In test paths or embedded usage, session_mgr may not be
    wired.  Eviction should still proceed."""
    old = _make_old_conn(device_id="dev-A")
    conns = {"ws-old": old}

    out = await evict_stale_connections_for_device(
        active_connections=conns,
        session_mgr=None,
        device_id="dev-A",
        new_ws_id="ws-new",
    )
    assert out == 1
    assert "ws-old" not in conns


@pytest.mark.asyncio
async def test_no_pipeline_on_old_conn_does_not_crash():
    """If the old conn never reached pipeline-init (boot race),
    we still evict cleanly."""
    old = _make_old_conn(device_id="dev-A", has_pipeline=False)
    conns = {"ws-old": old}

    out = await evict_stale_connections_for_device(
        active_connections=conns,
        session_mgr=_make_session_mgr(),
        device_id="dev-A",
        new_ws_id="ws-new",
    )
    assert out == 1


@pytest.mark.asyncio
async def test_no_on_event_on_old_conn_does_not_crash():
    """If the old conn never installed _on_event (test stub etc.),
    we skip the notify but still evict."""
    old = _make_old_conn(device_id="dev-A", has_on_event=False)
    conns = {"ws-old": old}

    out = await evict_stale_connections_for_device(
        active_connections=conns,
        session_mgr=_make_session_mgr(),
        device_id="dev-A",
        new_ws_id="ws-new",
    )
    assert out == 1


# ─── Multi-evict branch ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_multiple_stale_entries_all_evicted():
    """Pathological case: two old connections somehow ended up
    registered to the same device.  Both must be evicted."""
    old1 = _make_old_conn(device_id="dev-A", session_id="sess-1")
    old2 = _make_old_conn(device_id="dev-A", session_id="sess-2")
    sm = _make_session_mgr()
    conns = {"ws-1": old1, "ws-2": old2}

    out = await evict_stale_connections_for_device(
        active_connections=conns,
        session_mgr=sm,
        device_id="dev-A",
        new_ws_id="ws-new",
    )

    assert out == 2
    assert "ws-1" not in conns
    assert "ws-2" not in conns
    assert sm.pause_session.await_count == 2
