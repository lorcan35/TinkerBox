"""Test for C1 (#137): config_update rate-limit no longer silent.

Pre-fix the rate-limit branch in `_handle_config_update` was a bare
`logger.debug` + `return` — Tab5's mode-toggle UI sat on its previous
local state and the user assumed the swap landed.

Post-fix the branch emits a `config_update_rate_limited` γ-arch
TRANSIENT/SESSION error frame so Tab5 (γ2-H8) can render a toast.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from dragon_voice.config import VoiceConfig
from dragon_voice.server import VoiceServer


class _FakeWS:
    closed = False

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


def test_rate_limited_config_update_emits_gamma_arch_error() -> None:
    """Two config_update calls inside the 0.5 s window must produce
    exactly one structured error frame for the second call."""
    srv = VoiceServer(VoiceConfig())

    ws = _FakeWS()
    conn_state: dict[str, Any] = {"ws_id": "ws0"}
    cmd = {"type": "config_update", "voice_mode": 0}
    cfg = VoiceConfig()

    async def go():
        # First call sets the rate-limit timestamp; subsequent body
        # processing requires more state we don't want to set up — so
        # we manually seed the timestamp to 'just now' and send the
        # SECOND call to exercise the rate-limit branch.
        conn_state["_last_config_update_ts"] = time.monotonic()
        await srv._handle_config_update(ws, conn_state, cfg, cmd)

    asyncio.run(go())

    # Find the rate-limit error frame.
    errors = [s for s in ws.sent if s.get("type") == "error"]
    assert any(
        e.get("code") == "config_update_rate_limited" for e in errors
    ), f"expected config_update_rate_limited error, got sent={ws.sent!r}"
    err = next(e for e in errors if e.get("code") == "config_update_rate_limited")
    assert err.get("severity") == "transient"
    assert err.get("scope") == "session"
    assert "rate-limited" in err.get("message", "").lower() or "moment" in err.get("message", "").lower()


def test_first_config_update_is_not_rate_limited() -> None:
    """Defensive: the first call (no prior timestamp) must NOT emit
    the rate-limit error.  We can't easily run the full body in a
    unit test (requires DB + session manager + pipeline) so we just
    assert no rate-limit error in the first 1 ms of the call."""
    srv = VoiceServer(VoiceConfig())
    ws = _FakeWS()
    conn_state: dict[str, Any] = {"ws_id": "ws0"}
    cmd = {"type": "config_update", "voice_mode": 0}
    cfg = VoiceConfig()

    async def go():
        try:
            await srv._handle_config_update(ws, conn_state, cfg, cmd)
        except Exception:
            # Body will fail when it hits DB/pipeline calls — that's
            # fine, we only care that the rate-limit branch didn't fire.
            pass

    asyncio.run(go())

    rate_limit_errors = [
        s for s in ws.sent
        if s.get("type") == "error" and s.get("code") == "config_update_rate_limited"
    ]
    assert rate_limit_errors == [], (
        f"first call should not be rate-limited: sent={ws.sent!r}"
    )
