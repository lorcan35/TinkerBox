"""#341 / #342 — OAuth Device Authorization Grant client.

Uses aioresponses to mock the device-code + token endpoints.  Verifies
the full flow:

  1. POST device-code endpoint returns the challenge.
  2. First poll returns `authorization_pending` (200 with error body
     OR per RFC, 400 with structured error).
  3. Second poll returns tokens.
  4. `expires_in` is converted to absolute `expires_at`.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dragon_voice.tools.integrations.oauth import (
    DeviceCodeChallenge,
    DeviceCodeError,
    OAuthDeviceCodeClient,
    OAuthTokens,
)


@pytest.fixture
def fake_session():
    """Stand-in aiohttp ClientSession that records POSTs + returns
    pre-canned responses."""
    session = MagicMock()
    session.closed = False
    return session


def _mock_response(json_data: dict, status: int = 200):
    """Context-manager-shaped mock matching aiohttp.ClientSession.post()."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=MagicMock(
        status=status,
        json=AsyncMock(return_value=json_data),
    ))
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


@pytest.mark.asyncio
async def test_start_returns_challenge(fake_session):
    fake_session.post = MagicMock(return_value=_mock_response({
        "device_code": "DEV-CODE-1",
        "user_code": "ABC-123",
        "verification_url": "https://example.com/device",
        "verification_url_complete": "https://example.com/device?code=ABC-123",
        "expires_in": 1800,
        "interval": 5,
    }))
    client = OAuthDeviceCodeClient(
        client_id="cid",
        scope="scope-x",
        device_code_url="https://oauth.example.com/device",
        token_url="https://oauth.example.com/token",
        session=fake_session,
    )
    chal = await client.start()
    assert chal.device_code == "DEV-CODE-1"
    assert chal.user_code == "ABC-123"
    assert chal.verification_url_complete is not None
    assert chal.expires_in == 1800
    assert chal.interval == 5


@pytest.mark.asyncio
async def test_poll_once_pending_returns_none(fake_session):
    fake_session.post = MagicMock(return_value=_mock_response(
        {"error": "authorization_pending"}, status=428,
    ))
    client = OAuthDeviceCodeClient(
        client_id="cid", scope="s",
        device_code_url="https://x", token_url="https://x",
        session=fake_session,
    )
    result = await client.poll_once("DEV-CODE-1")
    assert result is None


@pytest.mark.asyncio
async def test_poll_once_success_returns_tokens(fake_session):
    fake_session.post = MagicMock(return_value=_mock_response({
        "access_token": "AT-1",
        "refresh_token": "RT-1",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "scope-a scope-b",
    }))
    client = OAuthDeviceCodeClient(
        client_id="cid", scope="s",
        device_code_url="https://x", token_url="https://x",
        session=fake_session,
    )
    t0 = time.time()
    tokens = await client.poll_once("DEV-CODE-1")
    assert tokens is not None
    assert tokens.access_token == "AT-1"
    assert tokens.refresh_token == "RT-1"
    assert tokens.token_type == "Bearer"
    assert tokens.expires_at >= t0 + 3500
    assert tokens.scopes == ["scope-a", "scope-b"]


@pytest.mark.asyncio
async def test_poll_once_terminal_error_raises(fake_session):
    fake_session.post = MagicMock(return_value=_mock_response({
        "error": "access_denied",
        "error_description": "user said no",
    }, status=400))
    client = OAuthDeviceCodeClient(
        client_id="cid", scope="s",
        device_code_url="https://x", token_url="https://x",
        session=fake_session,
    )
    with pytest.raises(DeviceCodeError) as excinfo:
        await client.poll_once("DEV-CODE-1")
    assert excinfo.value.code == "access_denied"


@pytest.mark.asyncio
async def test_refresh_returns_new_tokens(fake_session):
    fake_session.post = MagicMock(return_value=_mock_response({
        "access_token": "AT-2",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "s",
        # NOTE: no refresh_token in response — provider expects client
        # to reuse the old one.
    }))
    client = OAuthDeviceCodeClient(
        client_id="cid", scope="s",
        device_code_url="https://x", token_url="https://x",
        session=fake_session,
    )
    new_tokens = await client.refresh("RT-OLD")
    assert new_tokens.access_token == "AT-2"
    # Refresh-token-preservation invariant.
    assert new_tokens.refresh_token == "RT-OLD"


def test_is_expired_with_skew():
    t = OAuthTokens(
        access_token="x", refresh_token=None, token_type="Bearer",
        expires_at=int(time.time()) + 30, scopes=[],
    )
    # 60s skew window → 30s remaining counts as expired.
    assert t.is_expired(skew_s=60)
    # 10s skew → not yet expired.
    assert not t.is_expired(skew_s=10)


def test_oauth_tokens_serialization_roundtrip():
    original = OAuthTokens(
        access_token="AT",
        refresh_token="RT",
        token_type="Bearer",
        expires_at=1234567890,
        scopes=["a", "b"],
        extra={"id_token": "jwt..."},
    )
    revived = OAuthTokens.from_dict(original.to_dict())
    assert revived.access_token == original.access_token
    assert revived.refresh_token == original.refresh_token
    assert revived.expires_at == original.expires_at
    assert revived.scopes == original.scopes
    assert revived.extra == original.extra


@pytest.mark.asyncio
async def test_poll_until_done_loops_until_success(fake_session, monkeypatch):
    """Verify the convenience loop: pending → pending → success."""
    responses = [
        _mock_response({"error": "authorization_pending"}, status=428),
        _mock_response({"error": "authorization_pending"}, status=428),
        _mock_response({
            "access_token": "AT", "refresh_token": "RT",
            "token_type": "Bearer", "expires_in": 3600, "scope": "s",
        }),
    ]
    fake_session.post = MagicMock(side_effect=responses)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())  # don't actually sleep

    chal = DeviceCodeChallenge(
        device_code="DC",
        user_code="UC",
        verification_url="https://x",
        expires_in=1800,
        interval=1,  # cuts the poll budget
    )
    client = OAuthDeviceCodeClient(
        client_id="cid", scope="s",
        device_code_url="https://x", token_url="https://x",
        session=fake_session,
    )
    tokens = await client.poll_until_done(chal)
    assert tokens.access_token == "AT"
    assert tokens.refresh_token == "RT"
