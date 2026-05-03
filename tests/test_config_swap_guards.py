"""Tests for ``dragon_voice.config_swap_guards.validate_config_swap_prereqs``.

Pin every guard branch and the short-circuit chain.  Pre-extract these
were 80 LOC of inline early-return guards in
``server.py:_handle_config_update``; now each failure path has a
named test so a regression to the γ-arch error_event payloads or to
the revert-to-LOCAL behaviour gets caught at unit-test time.

Six branches:
  1. LOCAL mode: no checks needed → True, no WS sends.
  2. CLOUD mode + OR key present → True.
  3. CLOUD mode + OR key missing → False, error_event + revert sent.
  4. TINKERCLAW mode + gateway unreachable → False, error_event + revert.
  5. TINKERCLAW mode + gateway up + token missing → False, error_event + revert.
  6. TINKERCLAW mode + gateway up + token present → True.

The TC-gateway HTTP call is mocked with aioresponses-style monkey-patching
on aiohttp.ClientSession.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dragon_voice.config_swap_guards import validate_config_swap_prereqs
from dragon_voice.voice_modes import VoiceMode


def _make_ws(*, closed: bool = False) -> MagicMock:
    ws = MagicMock()
    ws.closed = closed
    return ws


def _make_conn_config(
    *,
    openrouter_api_key: str = "",
    tinkerclaw_token: str = "",
    tinkerclaw_url: str = "http://localhost:18789",
) -> MagicMock:
    cfg = MagicMock()
    cfg.llm.openrouter_api_key = openrouter_api_key
    cfg.llm.tinkerclaw_token = tinkerclaw_token
    cfg.llm.tinkerclaw_url = tinkerclaw_url
    return cfg


def _make_safe_send_json() -> AsyncMock:
    """Returns a callable mirroring VoiceServer._safe_send_json's
    signature.  Always returns True so the caller doesn't think the
    WS dropped mid-revert."""
    return AsyncMock(return_value=True)


class _FakeAsyncCM:
    """Minimal `async with` context-manager wrapper around an inner
    object.  ``async with X as inner`` returns the wrapped value;
    raises if `raise_on_enter` is set."""

    def __init__(self, inner: Any = None, raise_on_enter: BaseException | None = None):
        self._inner = inner
        self._raise = raise_on_enter

    async def __aenter__(self):
        if self._raise is not None:
            raise self._raise
        return self._inner

    async def __aexit__(self, exc_type, exc, tb):
        return False  # don't swallow


def _patch_aiohttp_session(*, status: int = 200, raise_on_get: BaseException | None = None):
    """Build a ClientSession patch that returns either a fake response
    (with the given .status) or raises from .get(...).
    Returns the patch context manager."""
    fake_resp = MagicMock()
    fake_resp.status = status

    fake_session = MagicMock()
    if raise_on_get is not None:
        # Calling tc_session.get(url) raises immediately — never reaches
        # the `async with` part.
        fake_session.get = MagicMock(side_effect=raise_on_get)
    else:
        # tc_session.get(url) returns an async-context-manager that
        # yields fake_resp on __aenter__.
        fake_session.get = MagicMock(return_value=_FakeAsyncCM(inner=fake_resp))

    # `async with aiohttp.ClientSession(...) as tc_session` returns
    # fake_session.  Wrap the whole ClientSession ctor so it returns an
    # async-CM that yields fake_session.
    return patch(
        "dragon_voice.config_swap_guards.aiohttp.ClientSession",
        return_value=_FakeAsyncCM(inner=fake_session),
    )


# ─── LOCAL — no checks at all ────────────────────────────────────


@pytest.mark.asyncio
async def test_local_mode_passes_with_no_checks():
    """LOCAL mode triggers neither TC nor OR-key checks → True with
    zero WS sends."""
    ws = _make_ws()
    cfg = _make_conn_config()
    send = _make_safe_send_json()

    out = await validate_config_swap_prereqs(
        ws,
        vmode=VoiceMode.LOCAL,
        conn_config=cfg,
        safe_send_json=send,
    )
    assert out is True
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_hybrid_mode_with_or_key_passes():
    ws = _make_ws()
    cfg = _make_conn_config(openrouter_api_key="sk-test-key")
    send = _make_safe_send_json()

    out = await validate_config_swap_prereqs(
        ws,
        vmode=VoiceMode.HYBRID,
        conn_config=cfg,
        safe_send_json=send,
    )
    assert out is True
    send.assert_not_awaited()


# ─── CLOUD/HYBRID without OR key — fails ─────────────────────────


@pytest.mark.asyncio
async def test_cloud_without_or_key_sends_error_event_and_revert():
    ws = _make_ws()
    cfg = _make_conn_config(openrouter_api_key="")  # explicitly missing
    send = _make_safe_send_json()

    out = await validate_config_swap_prereqs(
        ws,
        vmode=VoiceMode.CLOUD,
        conn_config=cfg,
        safe_send_json=send,
    )
    assert out is False
    # First send is the γ-arch error_event; second is the revert.
    assert send.await_count == 2
    err_payload = send.await_args_list[0].args[1]
    revert_payload = send.await_args_list[1].args[1]
    assert err_payload.get("type") == "error"
    assert err_payload.get("code") == "openrouter_key_missing"
    assert revert_payload == {"type": "config_update", "voice_mode": 0}


@pytest.mark.asyncio
async def test_or_key_check_skipped_when_ws_closed():
    """If the WS is already closed, we still return False but don't
    waste cycles trying to send frames the client can't receive."""
    ws = _make_ws(closed=True)
    cfg = _make_conn_config(openrouter_api_key="")
    send = _make_safe_send_json()

    out = await validate_config_swap_prereqs(
        ws,
        vmode=VoiceMode.HYBRID,
        conn_config=cfg,
        safe_send_json=send,
    )
    assert out is False
    send.assert_not_awaited()


# ─── TINKERCLAW gateway-unreachable ──────────────────────────────


@pytest.mark.asyncio
async def test_tinkerclaw_gateway_unreachable_sends_error_event_and_revert():
    """When the TC gateway HTTP call raises, the guard sends the
    γ-arch error + revert and returns False."""
    ws = _make_ws()
    cfg = _make_conn_config(tinkerclaw_token="tk-test-token")
    send = _make_safe_send_json()

    with _patch_aiohttp_session(raise_on_get=ConnectionRefusedError("gateway not running")):
        out = await validate_config_swap_prereqs(
            ws,
            vmode=VoiceMode.TINKERCLAW,
            conn_config=cfg,
            safe_send_json=send,
        )

    assert out is False
    assert send.await_count == 2
    err_payload = send.await_args_list[0].args[1]
    assert err_payload.get("code") == "tc_gateway_unreachable"


@pytest.mark.asyncio
async def test_tinkerclaw_gateway_returns_500_sends_error_event():
    """A non-200 status from the gateway is a failure too — pinned so
    a future "accept any status" regression gets caught."""
    ws = _make_ws()
    cfg = _make_conn_config(tinkerclaw_token="tk-test-token")
    send = _make_safe_send_json()

    with _patch_aiohttp_session(status=503):
        out = await validate_config_swap_prereqs(
            ws,
            vmode=VoiceMode.TINKERCLAW,
            conn_config=cfg,
            safe_send_json=send,
        )

    assert out is False
    assert send.await_count == 2
    assert send.await_args_list[0].args[1].get("code") == "tc_gateway_unreachable"


# ─── TINKERCLAW token missing ────────────────────────────────────


@pytest.mark.asyncio
async def test_tinkerclaw_with_blank_token_after_gateway_ok_sends_error():
    """If the gateway is up but the token is missing, the second
    guard fires and sends tc_token_missing."""
    ws = _make_ws()
    cfg = _make_conn_config(tinkerclaw_token="")  # explicitly blank
    send = _make_safe_send_json()

    with _patch_aiohttp_session(status=200):
        out = await validate_config_swap_prereqs(
            ws,
            vmode=VoiceMode.TINKERCLAW,
            conn_config=cfg,
            safe_send_json=send,
        )

    assert out is False
    # Two sends: error_event + revert (no OR-key guard for TC mode).
    assert send.await_count == 2
    assert send.await_args_list[0].args[1].get("code") == "tc_token_missing"


@pytest.mark.asyncio
async def test_tinkerclaw_with_whitespace_only_token_treated_as_blank():
    """`"   "` should be treated as blank — pin the .strip() invariant."""
    ws = _make_ws()
    cfg = _make_conn_config(tinkerclaw_token="   ")
    send = _make_safe_send_json()

    with _patch_aiohttp_session(status=200):
        out = await validate_config_swap_prereqs(
            ws,
            vmode=VoiceMode.TINKERCLAW,
            conn_config=cfg,
            safe_send_json=send,
        )

    assert out is False
    assert send.await_args_list[0].args[1].get("code") == "tc_token_missing"


# ─── TINKERCLAW happy path ───────────────────────────────────────


@pytest.mark.asyncio
async def test_tinkerclaw_with_gateway_ok_and_token_present_passes():
    ws = _make_ws()
    cfg = _make_conn_config(tinkerclaw_token="tk-test-token")
    send = _make_safe_send_json()

    with _patch_aiohttp_session(status=200):
        out = await validate_config_swap_prereqs(
            ws,
            vmode=VoiceMode.TINKERCLAW,
            conn_config=cfg,
            safe_send_json=send,
        )

    assert out is True
    send.assert_not_awaited()


# ─── Short-circuit invariant ─────────────────────────────────────


@pytest.mark.asyncio
async def test_tc_gateway_failure_short_circuits_token_check():
    """If the gateway check fails, the token check must NOT run —
    one error per attempt, not two."""
    ws = _make_ws()
    # Both would-fail: blank token AND unreachable gateway.
    cfg = _make_conn_config(tinkerclaw_token="")
    send = _make_safe_send_json()

    with _patch_aiohttp_session(raise_on_get=ConnectionRefusedError("gateway down")):
        out = await validate_config_swap_prereqs(
            ws,
            vmode=VoiceMode.TINKERCLAW,
            conn_config=cfg,
            safe_send_json=send,
        )

    assert out is False
    # Only the gateway-unreachable error fired; token check skipped.
    assert send.await_count == 2  # 1 error + 1 revert
    assert send.await_args_list[0].args[1].get("code") == "tc_gateway_unreachable"
