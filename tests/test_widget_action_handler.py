"""Tests for ``dragon_voice.widget_action_handler``.

Pin every guard branch + the audit-P0 (v4·D Phase 4g) closure
that prompt taps don't get silently dropped into the "Unknown
command" hole.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.widget_action_handler import handle_widget_action


def _make_surface_mgr() -> MagicMock:
    mgr = MagicMock()
    mgr.handle_action = AsyncMock()
    return mgr


# ─── Happy path ──────────────────────────────────────────────


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_full_dispatch_to_surface_mgr(self):
        mgr = _make_surface_mgr()
        payload = {"choice_id": "yes", "extra": 42}

        await handle_widget_action(
            cmd={
                "type": "widget_action",
                "card_id": "card-123",
                "event": "choice_picked",
                "payload": payload,
            },
            conn_state={"session_id": "sess-A"},
            surface_mgr=mgr,
        )

        mgr.handle_action.assert_awaited_once_with(
            "sess-A", "card-123", "choice_picked", payload,
        )

    @pytest.mark.asyncio
    async def test_missing_payload_defaults_to_empty_dict(self):
        """Tab5 may omit payload for buttons with no data — pin
        the default-to-{} semantic so SurfaceManager.handle_action
        never receives None."""
        mgr = _make_surface_mgr()

        await handle_widget_action(
            cmd={
                "card_id": "btn-1",
                "event": "tap",
                # no payload
            },
            conn_state={"session_id": "s"},
            surface_mgr=mgr,
        )

        mgr.handle_action.assert_awaited_once_with("s", "btn-1", "tap", {})


# ─── Guard branches ─────────────────────────────────────────


class TestGuards:
    @pytest.mark.asyncio
    async def test_no_session_id_skips_dispatch(self):
        """Pre-register widget_action (boot race) — skip silently."""
        mgr = _make_surface_mgr()

        await handle_widget_action(
            cmd={"card_id": "c", "event": "e"},
            conn_state={},  # no session_id
            surface_mgr=mgr,
        )

        mgr.handle_action.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_card_id_skips(self):
        mgr = _make_surface_mgr()

        await handle_widget_action(
            cmd={"event": "e"},  # no card_id
            conn_state={"session_id": "s"},
            surface_mgr=mgr,
        )

        mgr.handle_action.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_event_skips(self):
        mgr = _make_surface_mgr()

        await handle_widget_action(
            cmd={"card_id": "c"},  # no event
            conn_state={"session_id": "s"},
            surface_mgr=mgr,
        )

        mgr.handle_action.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_surface_mgr_skips(self):
        """Test path / boot race — silent skip when surface_mgr
        not available."""
        await handle_widget_action(
            cmd={"card_id": "c", "event": "e"},
            conn_state={"session_id": "s"},
            surface_mgr=None,
        )
        # No assertion — must not raise.


# ─── Failure isolation ──────────────────────────────────────


class TestFailureIsolation:
    @pytest.mark.asyncio
    async def test_skill_exception_does_not_propagate(self):
        """Audit invariant: a buggy skill's handle_action exception
        MUST NOT tear down the WS read loop.  Pin so a future
        refactor can't drop the try/except."""
        mgr = MagicMock()
        mgr.handle_action = AsyncMock(
            side_effect=RuntimeError("skill blew up"),
        )

        # Must NOT raise.
        await handle_widget_action(
            cmd={"card_id": "c", "event": "e"},
            conn_state={"session_id": "s"},
            surface_mgr=mgr,
        )

    @pytest.mark.asyncio
    async def test_skill_exception_still_calls_handle_action(self):
        """The dispatch attempt fires even if handle_action raises
        — verify the call was actually made."""
        mgr = MagicMock()
        mgr.handle_action = AsyncMock(
            side_effect=RuntimeError("skill blew up"),
        )

        await handle_widget_action(
            cmd={"card_id": "c", "event": "tap"},
            conn_state={"session_id": "s"},
            surface_mgr=mgr,
        )

        mgr.handle_action.assert_awaited_once()


# ─── Audit P0 closure pin ────────────────────────────────────


class TestAuditP0Closure:
    @pytest.mark.asyncio
    async def test_widget_action_actually_dispatches(self):
        """v4·D Phase 4g audit P0 closure: pre-fix every interactive
        widget tap was silently dropped into the "Unknown command"
        logger.  Pin that with valid inputs the handler ACTUALLY
        forwards to SurfaceManager (not just no-ops at WARN)."""
        mgr = _make_surface_mgr()

        await handle_widget_action(
            cmd={"card_id": "prompt-42", "event": "choice_a"},
            conn_state={"session_id": "live"},
            surface_mgr=mgr,
        )

        # The whole point of the branch existing — handle_action
        # MUST be called (not silently skipped).
        mgr.handle_action.assert_awaited_once()
