"""#341 / #345 — OAuth Authorization-Code-with-PKCE client.

Pairs with `test_oauth_device_code.py` (the device-code path is kept
for non-Google providers).  This file covers the new auth-code client
that Google Calendar + Gmail use.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
import urllib.parse
from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.tools.integrations.oauth import (
    AuthCodeChallenge,
    DeviceCodeError,
    OAuthAuthCodeClient,
    OAuthTokens,
    _pkce_pair,
)


@pytest.fixture
def fake_session():
    """aiohttp ClientSession stand-in — records POSTs + returns pre-canned responses."""
    session = MagicMock()
    session.closed = False
    return session


def _mock_response(json_data: dict, status: int = 200):
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=MagicMock(
        status=status,
        json=AsyncMock(return_value=json_data),
    ))
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _make_client(session) -> OAuthAuthCodeClient:
    return OAuthAuthCodeClient(
        client_id="cid",
        client_secret="csec",
        scope="scope-x scope-y",
        auth_url="https://accounts.example.com/auth",
        token_url="https://oauth.example.com/token",
        redirect_uri="https://callback.example.com/cb",
        session=session,
    )


# ── _pkce_pair ──────────────────────────────────────────────────────


def test_pkce_pair_verifier_meets_rfc_7636_length():
    verifier, _challenge = _pkce_pair()
    # RFC 7636 §4.1: verifier is 43–128 chars in [A-Za-z0-9-._~].
    assert 43 <= len(verifier) <= 128
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
    assert set(verifier).issubset(allowed)


def test_pkce_pair_challenge_is_sha256_of_verifier():
    verifier, challenge = _pkce_pair()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest(),
    ).rstrip(b"=").decode("ascii")
    assert challenge == expected


def test_pkce_pair_yields_unique_values():
    pairs = [_pkce_pair() for _ in range(5)]
    verifiers = {v for v, _ in pairs}
    assert len(verifiers) == 5


# ── start() ─────────────────────────────────────────────────────────


def test_start_returns_authorization_url_with_pkce_and_state(fake_session):
    client = _make_client(fake_session)
    chal = client.start()
    assert isinstance(chal, AuthCodeChallenge)

    parsed = urllib.parse.urlparse(chal.authorization_url)
    params = dict(urllib.parse.parse_qsl(parsed.query))
    assert params["client_id"] == "cid"
    assert params["redirect_uri"] == "https://callback.example.com/cb"
    assert params["response_type"] == "code"
    assert params["scope"] == "scope-x scope-y"
    assert params["code_challenge_method"] == "S256"
    assert params["state"] == chal.state
    # The challenge in the URL is BASE64URL(SHA256(verifier)).
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(chal.code_verifier.encode("ascii")).digest(),
    ).rstrip(b"=").decode("ascii")
    assert params["code_challenge"] == expected


def test_start_applies_provider_extras(fake_session):
    client = _make_client(fake_session)
    chal = client.start(extra_params={
        "access_type": "offline",
        "prompt": "consent",
    })
    parsed = urllib.parse.urlparse(chal.authorization_url)
    params = dict(urllib.parse.parse_qsl(parsed.query))
    assert params["access_type"] == "offline"
    assert params["prompt"] == "consent"


def test_start_registers_pending_state_with_verifier(fake_session):
    client = _make_client(fake_session)
    chal = client.start()
    entry = client._pending[chal.state]
    assert entry["code_verifier"] == chal.code_verifier
    assert entry["future"] is not None


# ── resolve_callback + wait_for_callback ────────────────────────────


@pytest.mark.asyncio
async def test_wait_for_callback_returns_code_after_resolve(fake_session):
    client = _make_client(fake_session)
    chal = client.start()

    async def _resolver():
        await asyncio.sleep(0)  # let the waiter park
        client.resolve_callback(state=chal.state, code="AUTH-CODE-1", error=None)

    code, _ = await asyncio.gather(
        client.wait_for_callback(chal.state, timeout_s=5),
        _resolver(),
    )
    assert code == "AUTH-CODE-1"


@pytest.mark.asyncio
async def test_wait_for_callback_raises_on_provider_error(fake_session):
    client = _make_client(fake_session)
    chal = client.start()

    async def _resolver():
        await asyncio.sleep(0)
        client.resolve_callback(state=chal.state, code=None, error="access_denied")

    with pytest.raises(DeviceCodeError) as excinfo:
        await asyncio.gather(
            client.wait_for_callback(chal.state, timeout_s=5),
            _resolver(),
        )
    assert excinfo.value.code == "access_denied"


@pytest.mark.asyncio
async def test_wait_for_callback_times_out_as_expired_token(fake_session):
    client = _make_client(fake_session)
    chal = client.start()
    with pytest.raises(DeviceCodeError) as excinfo:
        # Tiny timeout — the resolver never runs.
        await client.wait_for_callback(chal.state, timeout_s=0)
    assert excinfo.value.code == "expired_token"


def test_resolve_callback_unknown_state_does_not_raise(fake_session):
    """Stale/forged states should log a warning, not crash the callback."""
    client = _make_client(fake_session)
    client.resolve_callback(state="never-issued", code="x", error=None)
    # Test passes if the call returned cleanly.


def test_wait_for_callback_unknown_state_raises_immediately(fake_session):
    client = _make_client(fake_session)
    with pytest.raises(DeviceCodeError) as excinfo:
        # No coroutine awaited — the raise is synchronous.
        asyncio.get_event_loop().run_until_complete(
            client.wait_for_callback("never-issued", timeout_s=1),
        )
    assert excinfo.value.code == "unknown_state"


# ── exchange_code_with_verifier ─────────────────────────────────────


@pytest.mark.asyncio
async def test_exchange_code_with_verifier_posts_pkce_payload(fake_session):
    fake_session.post = MagicMock(return_value=_mock_response({
        "access_token": "AT-1",
        "refresh_token": "RT-1",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "scope-a scope-b",
    }))
    client = _make_client(fake_session)
    tokens = await client.exchange_code_with_verifier(
        code="AUTH-CODE-1", code_verifier="VERIFIER-X",
    )
    assert isinstance(tokens, OAuthTokens)
    assert tokens.access_token == "AT-1"
    assert tokens.refresh_token == "RT-1"
    assert tokens.scopes == ["scope-a", "scope-b"]

    payload = fake_session.post.call_args.kwargs["data"]
    assert payload["grant_type"] == "authorization_code"
    assert payload["code"] == "AUTH-CODE-1"
    assert payload["code_verifier"] == "VERIFIER-X"
    assert payload["client_id"] == "cid"
    assert payload["client_secret"] == "csec"
    assert payload["redirect_uri"] == "https://callback.example.com/cb"


@pytest.mark.asyncio
async def test_exchange_code_raises_on_provider_400(fake_session):
    fake_session.post = MagicMock(return_value=_mock_response(
        {"error": "invalid_grant", "error_description": "code reused"},
        status=400,
    ))
    client = _make_client(fake_session)
    with pytest.raises(DeviceCodeError) as excinfo:
        await client.exchange_code_with_verifier(code="x", code_verifier="v")
    assert excinfo.value.code == "invalid_grant"


# ── refresh ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_returns_new_access_token(fake_session):
    fake_session.post = MagicMock(return_value=_mock_response({
        "access_token": "AT-2",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "scope-a",
    }))
    client = _make_client(fake_session)
    t0 = time.time()
    new_tokens = await client.refresh("RT-OLD")
    assert new_tokens.access_token == "AT-2"
    assert new_tokens.expires_at >= t0 + 3500
    # Server omitted refresh_token → preserve the prior one.
    assert new_tokens.refresh_token == "RT-OLD"


@pytest.mark.asyncio
async def test_refresh_raises_on_provider_rejection(fake_session):
    fake_session.post = MagicMock(return_value=_mock_response(
        {"error": "invalid_grant"}, status=400,
    ))
    client = _make_client(fake_session)
    with pytest.raises(DeviceCodeError) as excinfo:
        await client.refresh("RT-DEAD")
    assert excinfo.value.code == "invalid_grant"
