"""#341 / #342 / #346 / #347 — Google Calendar integration (multi-account).

Stubs the Google OAuth + Calendar API via aiohttp session mocks.
Covers:
  * Multi-account internals (_accounts dict + default_account_id)
  * `auth_kind` is `oauth-authcode`
  * `start_connect` builds a PKCE authorization_url + tracks the flow
  * `handle_callback` resolves account_id from id_token / userinfo /
    placeholder, persists tokens under that key
  * First connect sets default; second connect leaves default alone
  * `set_default_account` moves the flag
  * `disconnect(account_id)` and `disconnect()` semantics
  * `list_events` / `create_event` round-trip with multi-account internals
  * Legacy single-account file is migrated to `_legacy.json` on first load
"""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dragon_voice.tools.integrations.credentials import (
    LEGACY_ACCOUNT_ID,
    ProviderCredentialDir,
)
from dragon_voice.tools.integrations.google.calendar import (
    PROVIDER_NAME,
    GoogleCalendarIntegration,
)
from dragon_voice.tools.integrations.oauth import DeviceCodeError, OAuthTokens


@pytest.fixture(autouse=True)
def _redirect_cred_store(tmp_path, monkeypatch):
    monkeypatch.setenv("TINKERCLAW_INTEGRATIONS_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def _oauth_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "test-csec")


@pytest.fixture
def integration(tmp_path):
    integ = GoogleCalendarIntegration()
    integ._provider_dir = ProviderCredentialDir(PROVIDER_NAME, base_dir=tmp_path)
    return integ


def _tokens(access="AT-1", refresh="RT-1", scope_overrides=None, id_email=None):
    """Build OAuthTokens, optionally with an id_token claiming `id_email`."""
    extra = {}
    if id_email:
        payload = {
            "email": id_email,
            "email_verified": True,
            "sub": "1234567890",
        }
        body_b64 = base64.urlsafe_b64encode(
            json.dumps(payload).encode("ascii"),
        ).rstrip(b"=").decode("ascii")
        extra["id_token"] = f"header.{body_b64}.signature"
    return OAuthTokens(
        access_token=access,
        refresh_token=refresh,
        token_type="Bearer",
        expires_at=int(time.time()) + 3600,
        scopes=scope_overrides or [
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/calendar.events",
            "openid", "email",
        ],
        extra=extra,
    )


def _make_mock_response(json_data, status=200):
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=MagicMock(
        status=status,
        json=AsyncMock(return_value=json_data),
        raise_for_status=MagicMock(),
    ))
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


async def _persist(integ, account_id, tokens, default=False):
    """Helper to put an account in the integration's state + on disk."""
    await integ._ensure_loaded()
    if tokens.extra is None:
        tokens.extra = {}
    tokens.extra["default"] = default
    await integ._persist_account(account_id, tokens)
    integ._accounts[account_id] = tokens
    if default or integ._default_account_id is None:
        integ._default_account_id = account_id


# ── shape ───────────────────────────────────────────────────────────


def test_auth_kind_is_oauth_authcode(integration):
    assert integration.auth_kind == "oauth-authcode"


def test_supports_multi_account(integration):
    assert integration.supports_multi_account is True


# ── lifecycle: empty state ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_initial_state_is_disconnected(integration):
    assert await integration.is_connected() is False
    assert await integration.list_accounts() == []


@pytest.mark.asyncio
async def test_health_check_returns_false_when_disconnected(integration):
    ok, detail = await integration.health_check()
    assert ok is False
    assert "not connected" in detail.lower()


# ── start_connect ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_connect_requires_oauth_env(integration, monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    from dragon_voice.tools.integrations.google.auth import (
        GoogleOAuthNotConfiguredError,
    )
    with pytest.raises(GoogleOAuthNotConfiguredError):
        await integration.start_connect()


@pytest.mark.asyncio
async def test_start_connect_returns_pkce_authorization_url(integration):
    chal = await integration.start_connect()
    assert chal.kind == "oauth-authcode"
    assert chal.request_id
    assert chal.verification_url.startswith(
        "https://accounts.google.com/o/oauth2/v2/auth?",
    )
    assert "code_challenge_method=S256" in chal.verification_url
    assert "access_type=offline" in chal.verification_url
    # openid + email scopes are auto-included so we can extract account_id.
    assert "openid" in chal.verification_url
    assert "email" in chal.verification_url
    assert chal.user_code is None


@pytest.mark.asyncio
async def test_start_connect_tracks_flow_by_state(integration):
    chal = await integration.start_connect()
    flow = integration._flows[chal.request_id]
    assert integration._find_flow_by_state(flow.state) is flow


# ── handle_callback paths ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_callback_unknown_state_is_noop(integration):
    await integration.handle_callback(state="never-issued", code="x", error=None)
    assert await integration.is_connected() is False


@pytest.mark.asyncio
async def test_handle_callback_with_error_sets_flow_error(integration):
    chal = await integration.start_connect()
    flow = integration._flows[chal.request_id]
    await integration.handle_callback(
        state=flow.state, code=None, error="access_denied",
    )
    assert flow.error is not None
    assert flow.error.code == "access_denied"
    status = await integration.poll_status(chal.request_id)
    assert status.state == "error"


@pytest.mark.asyncio
async def test_handle_callback_uses_id_token_email_as_account_id(integration):
    chal = await integration.start_connect()
    flow = integration._flows[chal.request_id]
    tokens = _tokens(id_email="me@example.com")

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.exchange_code_with_verifier = AsyncMock(return_value=tokens)

    with patch(
        "dragon_voice.tools.integrations.google.calendar.make_google_client",
        return_value=fake_client,
    ):
        await integration.handle_callback(
            state=flow.state, code="AUTH-1", error=None,
        )

    accounts = await integration.list_accounts()
    assert len(accounts) == 1
    assert accounts[0].account_id == "me@example.com"
    assert accounts[0].default is True
    status = await integration.poll_status(chal.request_id)
    assert status.state == "connected"
    assert status.account_id == "me@example.com"


@pytest.mark.asyncio
async def test_handle_callback_falls_back_to_userinfo_when_id_token_missing(
    integration,
):
    chal = await integration.start_connect()
    flow = integration._flows[chal.request_id]
    tokens = _tokens()  # no id_email → no id_token

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.exchange_code_with_verifier = AsyncMock(return_value=tokens)

    with patch(
        "dragon_voice.tools.integrations.google.calendar.make_google_client",
        return_value=fake_client,
    ), patch(
        "dragon_voice.tools.integrations.google.calendar.fetch_google_email",
        new=AsyncMock(return_value="userinfo@example.com"),
    ):
        await integration.handle_callback(
            state=flow.state, code="AUTH-1", error=None,
        )

    accounts = await integration.list_accounts()
    assert [a.account_id for a in accounts] == ["userinfo@example.com"]


@pytest.mark.asyncio
async def test_handle_callback_uses_placeholder_when_both_id_sources_fail(
    integration,
):
    chal = await integration.start_connect()
    flow = integration._flows[chal.request_id]
    tokens = _tokens()

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.exchange_code_with_verifier = AsyncMock(return_value=tokens)

    with patch(
        "dragon_voice.tools.integrations.google.calendar.make_google_client",
        return_value=fake_client,
    ), patch(
        "dragon_voice.tools.integrations.google.calendar.fetch_google_email",
        new=AsyncMock(return_value=None),
    ):
        await integration.handle_callback(
            state=flow.state, code="AUTH-1", error=None,
        )

    accounts = await integration.list_accounts()
    assert len(accounts) == 1
    assert accounts[0].account_id.startswith("pending-")


@pytest.mark.asyncio
async def test_second_connect_does_not_unseat_first_default(integration):
    await _persist(integration, "first@example.com", _tokens(), default=True)

    chal = await integration.start_connect()
    flow = integration._flows[chal.request_id]
    second = _tokens(id_email="second@example.com")

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.exchange_code_with_verifier = AsyncMock(return_value=second)

    with patch(
        "dragon_voice.tools.integrations.google.calendar.make_google_client",
        return_value=fake_client,
    ):
        await integration.handle_callback(
            state=flow.state, code="AUTH-2", error=None,
        )

    accounts = {a.account_id: a for a in await integration.list_accounts()}
    assert set(accounts.keys()) == {"first@example.com", "second@example.com"}
    assert accounts["first@example.com"].default is True
    assert accounts["second@example.com"].default is False


# ── set_default_account / disconnect ───────────────────────────────


@pytest.mark.asyncio
async def test_set_default_account_moves_the_flag(integration, tmp_path):
    await _persist(integration, "first@example.com", _tokens(), default=True)
    await _persist(integration, "second@example.com", _tokens())

    await integration.set_default_account("second@example.com")

    # Reload from disk to verify persistence.
    fresh = GoogleCalendarIntegration()
    fresh._provider_dir = ProviderCredentialDir(PROVIDER_NAME, base_dir=tmp_path)
    await fresh._ensure_loaded()
    accounts = {a.account_id: a for a in await fresh.list_accounts()}
    assert accounts["second@example.com"].default is True
    assert accounts["first@example.com"].default is False


@pytest.mark.asyncio
async def test_set_default_account_unknown_raises(integration):
    await _persist(integration, "first@example.com", _tokens(), default=True)
    with pytest.raises(DeviceCodeError) as excinfo:
        await integration.set_default_account("ghost@example.com")
    assert excinfo.value.code == "unknown_account"


@pytest.mark.asyncio
async def test_disconnect_one_account_leaves_others(integration):
    await _persist(integration, "first@example.com", _tokens(refresh=None), default=True)
    await _persist(integration, "second@example.com", _tokens(refresh=None))

    await integration.disconnect(account_id="first@example.com")

    accounts = await integration.list_accounts()
    assert [a.account_id for a in accounts] == ["second@example.com"]
    assert accounts[0].default is True
    assert await integration.is_connected("first@example.com") is False
    assert await integration.is_connected("second@example.com") is True


@pytest.mark.asyncio
async def test_disconnect_with_no_account_id_clears_all(integration):
    await _persist(integration, "first@example.com", _tokens(refresh=None), default=True)
    await _persist(integration, "second@example.com", _tokens(refresh=None))

    await integration.disconnect()

    assert await integration.list_accounts() == []
    assert await integration.is_connected() is False


# ── Legacy migration ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_legacy_flat_file_migrated_on_first_load(tmp_path):
    """Pre-#347 builds wrote ``{base}/{provider}.json``.  The first load
    after upgrade must move it to ``{provider}/_legacy.json`` and
    surface it as the default account."""
    flat = tmp_path / f"{PROVIDER_NAME}.json"
    flat.write_text(json.dumps({"tokens": _tokens().to_dict()}))

    integ = GoogleCalendarIntegration()
    integ._provider_dir = ProviderCredentialDir(PROVIDER_NAME, base_dir=tmp_path)
    await integ._ensure_loaded()

    accounts = await integ.list_accounts()
    assert [a.account_id for a in accounts] == [LEGACY_ACCOUNT_ID]
    assert accounts[0].default is True
    assert not flat.exists()
    assert (tmp_path / PROVIDER_NAME / f"{LEGACY_ACCOUNT_ID}.json").exists()


# ── Calendar API (multi-account aware) ─────────────────────────────


@pytest.mark.asyncio
async def test_list_events_routes_to_default_when_account_omitted(integration):
    await _persist(integration, "first@example.com", _tokens(), default=True)
    await _persist(integration, "second@example.com", _tokens(access="AT-OTHER"))

    fake_session = MagicMock()
    fake_session.closed = False
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    fake_session.get = MagicMock(return_value=_make_mock_response({
        "items": [
            {"id": "evt-1", "summary": "Dentist",
             "start": {"dateTime": "2026-05-17T14:00:00Z"},
             "end": {"dateTime": "2026-05-17T15:00:00Z"}},
        ],
    }))

    with patch(
        "dragon_voice.tools.integrations.google.calendar.aiohttp.ClientSession",
        return_value=fake_session,
    ):
        events = await integration.list_events()

    assert len(events) == 1
    assert events[0]["account"] == "first@example.com"
    headers = fake_session.get.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer AT-1"


@pytest.mark.asyncio
async def test_list_events_with_explicit_account_routes_correctly(integration):
    await _persist(integration, "first@example.com", _tokens(access="AT-FIRST"), default=True)
    await _persist(integration, "second@example.com", _tokens(access="AT-SECOND"))

    fake_session = MagicMock()
    fake_session.closed = False
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    fake_session.get = MagicMock(return_value=_make_mock_response({"items": []}))

    with patch(
        "dragon_voice.tools.integrations.google.calendar.aiohttp.ClientSession",
        return_value=fake_session,
    ):
        await integration.list_events(account_id="second@example.com")

    headers = fake_session.get.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer AT-SECOND"


@pytest.mark.asyncio
async def test_create_event_posts_with_default_account_token(integration):
    await _persist(integration, "first@example.com", _tokens(), default=True)

    fake_session = MagicMock()
    fake_session.closed = False
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    fake_session.post = MagicMock(return_value=_make_mock_response({
        "id": "evt-new", "summary": "Lunch",
        "start": {"dateTime": "2026-05-17T12:00:00Z"},
        "end": {"dateTime": "2026-05-17T13:00:00Z"},
    }))

    with patch(
        "dragon_voice.tools.integrations.google.calendar.aiohttp.ClientSession",
        return_value=fake_session,
    ):
        ev = await integration.create_event(
            summary="Lunch",
            start=datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc),
            end=datetime(2026, 5, 17, 13, 0, tzinfo=timezone.utc),
        )

    assert ev["id"] == "evt-new"
    assert ev["account"] == "first@example.com"
    call_kwargs = fake_session.post.call_args.kwargs
    assert call_kwargs["headers"]["Authorization"] == "Bearer AT-1"


@pytest.mark.asyncio
async def test_expired_token_triggers_refresh_per_account(integration):
    """When the default account's token expires, refresh fires only
    for that account; the other account's tokens are untouched."""
    expired_first = _tokens(access="AT-OLD", refresh="RT-1")
    expired_first.expires_at = int(time.time()) - 10
    await _persist(integration, "first@example.com", expired_first, default=True)
    untouched = _tokens(access="AT-OTHER-STILL-GOOD", refresh="RT-OTHER")
    await _persist(integration, "second@example.com", untouched)

    new_tokens = _tokens(access="AT-NEW", refresh="RT-1")

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.refresh = AsyncMock(return_value=new_tokens)

    with patch(
        "dragon_voice.tools.integrations.google.calendar.make_google_client",
        return_value=fake_client,
    ):
        token = await integration._get_access_token()

    assert token == "AT-NEW"
    fake_client.refresh.assert_awaited_once_with("RT-1")
    assert integration._accounts["second@example.com"].access_token == "AT-OTHER-STILL-GOOD"
