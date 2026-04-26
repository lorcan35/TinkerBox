"""Test for D5 (#137): cloud-mode missing-key + TC-gateway-unreachable
checks emit γ-arch error_event matching the B7 (TC token) shape.

Pre-fix both paths used a bare `config_update.error` raw-string —
inconsistent with the B7 fix and the audit's "γ-arch hygiene
completion" cross-cutting note.
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


def test_missing_openrouter_key_emits_gamma_arch_error_event() -> None:
    srv = VoiceServer(VoiceConfig())
    ws = _FakeWS()
    conn_state: dict[str, Any] = {"ws_id": "ws0"}
    cfg = VoiceConfig()
    cfg.llm.openrouter_api_key = ""  # explicitly missing
    cmd = {"type": "config_update", "voice_mode": 1}  # Hybrid

    async def go():
        await srv._handle_config_update(ws, conn_state, cfg, cmd)

    asyncio.run(go())

    errors = [s for s in ws.sent if s.get("type") == "error"]
    assert any(e.get("code") == "openrouter_key_missing" for e in errors), (
        f"expected openrouter_key_missing γ-arch error, got {ws.sent!r}"
    )
    err = next(e for e in errors if e.get("code") == "openrouter_key_missing")
    assert err.get("severity") == "fatal"
    assert err.get("scope") == "llm"
    assert "reverted to local" in err.get("message", "").lower()

    # Revert frame must be present and must NOT carry the legacy `error` key.
    reverts = [
        s for s in ws.sent
        if s.get("type") == "config_update" and s.get("voice_mode") == 0
    ]
    assert reverts, f"expected config_update revert, got {ws.sent!r}"
    assert "error" not in reverts[0], (
        f"revert frame leaked legacy `error` key: {reverts[0]!r}"
    )
