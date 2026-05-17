"""#341 / #342 / #346 — Google Calendar integration tests (auth-code shape).

Stubs the Google OAuth + Calendar API via aiohttp session mocks.
Asserts:
  * `auth_kind` is `oauth-authcode` (post-pivot)
  * `is_connected` is False initially, True after token persist
  * `start_connect` builds a PKCE authorization_url + tracks the flow
  * `handle_callback` resolves matching state, persists tokens
  * `poll_status` reports the flow state correctly
  * `list_events` / `create_event` / `disconnect` round-trip
  * Expired access token triggers refresh-on-401 via the auth-code client
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dragon_voice.tools.integrations.credentials import CredentialStore
from dragon_voice.tools.integrations.google.calendar import (
    GoogleCalendarIntegration,
)
from dragon_voice.tools.integrations.oauth import DeviceCodeError, OAuthTokens


@pytest.fixture(autouse=True)
def _redirect_cred_store(tmp_path, monkeypatch):
    """Make all CredentialStore writes land in a per-test tmp dir."""
    monkeypatch.setenv("TINKERCLAW_INTEGRATIONS_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def _oauth_env(monkeypatch):
    """Default Google OAuth client env so start_connect can build a URL."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "test-csec")


@pytest.fixture
def integration(tmp_path):
    integ = GoogleCalendarIntegration()
    integ._store = CredentialStore("google-calendar", base_dir=tmp_path)
    return integ


@pytest.fixture
def valid_tokens():
    return OAuthTokens(
        access_token="AT-fresh",
        refresh_token="RT-1",
        token_type="Bearer",
        expires_at=int(time.time()) + 3600,
        scopes=[
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/calendar.events",
        ],
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


# ── shape + lifecycle ───────────────────────────────────────────────


def test_auth_kind_is_oauth_authcode(integration):
    assert integration.auth_kind == "oauth-authcode"


@pytest.mark.asyncio
async def test_initial_state_is_disconnected(integration):
    assert await integration.is_connected() is False


@pytest.mark.asyncio
async def test_is_connected_true_after_persist(integration, valid_tokens):
    await integration._persist_tokens(valid_tokens)
    assert await integration.is_connected() is True


@pytest.mark.asyncio
async def test_health_check_returns_false_when_disconnected(integration):
    ok, detail = await integration.health_check()
    assert ok is False
    assert "not connected" in detail.lower()


# ── start_connect / poll_status / handle_callback ───────────────────


@pytest.mark.asyncio
async def test_start_connect_requires_oauth_env(integration, monkeypatch):
    """No GOOGLE_OAUTH_CLIENT_ID → raises so the REST layer returns 503."""
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    from dragon_voice.tools.integrations.google.auth import (
        GoogleOAuthNotConfiguredError,
    )
    with pytest.raises(GoogleOAuthNotConfiguredError):
        await integration.start_connect()


@pytest.mark.asyncio
async def test_start_connect_returns_pkce_authorization_url(integration):
    chal = await integration.start_connect()
    assert chal.request_id
    assert chal.verification_url.startswith(
        "https://accounts.google.com/o/oauth2/v2/auth?",
    )
    assert "code_challenge=" in chal.verification_url
    assert "code_challenge_method=S256" in chal.verification_url
    # Google needs offline + consent to mint a refresh_token.
    assert "access_type=offline" in chal.verification_url
    assert "prompt=consent" in chal.verification_url
    # Auth-code flow has no user_code (only device-code does).
    assert chal.user_code is None


@pytest.mark.asyncio
async def test_start_connect_tracks_flow_by_request_id_and_state(integration):
    chal = await integration.start_connect()
    flow = integration._flows[chal.request_id]
    assert flow.state  # populated
    assert flow.code_verifier  # populated
    # _find_flow_by_state agrees.
    assert integration._find_flow_by_state(flow.state) is flow


@pytest.mark.asyncio
async def test_find_flow_by_state_returns_none_for_unknown_state(integration):
    await integration.start_connect()
    assert integration._find_flow_by_state("never-issued") is None


@pytest.mark.asyncio
async def test_poll_status_reports_connecting_then_connected(
    integration, valid_tokens,
):
    chal = await integration.start_connect()
    status = await integration.poll_status(chal.request_id)
    assert status.state == "connecting"

    # Simulate the callback resolving the flow.
    flow = integration._flows[chal.request_id]
    flow.tokens = valid_tokens

    status = await integration.poll_status(chal.request_id)
    assert status.state == "connected"


@pytest.mark.asyncio
async def test_poll_status_unknown_request_id_is_error(integration):
    status = await integration.poll_status("never-issued")
    assert status.state == "error"
    assert "unknown" in (status.error or "").lower()


@pytest.mark.asyncio
async def test_handle_callback_unknown_state_is_noop(integration):
    # Must not raise; must not persist anything.
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
async def test_handle_callback_success_exchanges_and_persists(
    integration, valid_tokens,
):
    chal = await integration.start_connect()
    flow = integration._flows[chal.request_id]

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.exchange_code_with_verifier = AsyncMock(return_value=valid_tokens)

    with patch(
        "dragon_voice.tools.integrations.google.calendar.make_google_client",
        return_value=fake_client,
    ):
        await integration.handle_callback(
            state=flow.state, code="AUTH-CODE-1", error=None,
        )

    fake_client.exchange_code_with_verifier.assert_awaited_once_with(
        code="AUTH-CODE-1", code_verifier=flow.code_verifier,
    )
    assert flow.tokens is valid_tokens
    assert await integration.is_connected() is True

    status = await integration.poll_status(chal.request_id)
    assert status.state == "connected"


@pytest.mark.asyncio
async def test_handle_callback_exchange_failure_sets_flow_error(integration):
    chal = await integration.start_connect()
    flow = integration._flows[chal.request_id]

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.exchange_code_with_verifier = AsyncMock(
        side_effect=DeviceCodeError("invalid_grant", "code reused"),
    )

    with patch(
        "dragon_voice.tools.integrations.google.calendar.make_google_client",
        return_value=fake_client,
    ):
        await integration.handle_callback(
            state=flow.state, code="AUTH-CODE-1", error=None,
        )

    assert flow.tokens is None
    assert flow.error is not None
    assert flow.error.code == "invalid_grant"


# ── Calendar API (unchanged by the auth-code pivot) ────────────────


@pytest.mark.asyncio
async def test_list_events_returns_normalized_shape(integration, valid_tokens):
    """The Calendar API returns verbose payloads — we normalize down."""
    await integration._persist_tokens(valid_tokens)

    fake_session = MagicMock()
    fake_session.closed = False
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    fake_session.get = MagicMock(return_value=_make_mock_response({
        "items": [
            {
                "id": "evt-1",
                "summary": "Dentist",
                "location": "Geneva",
                "start": {"dateTime": "2026-05-17T14:00:00Z"},
                "end": {"dateTime": "2026-05-17T15:00:00Z"},
            },
            {
                "id": "evt-2",
                "summary": "Conference",
                "start": {"date": "2026-05-18"},
                "end": {"date": "2026-05-19"},
            },
        ],
    }))

    with patch(
        "dragon_voice.tools.integrations.google.calendar.aiohttp.ClientSession",
        return_value=fake_session,
    ):
        events = await integration.list_events()

    assert len(events) == 2
    assert events[0]["id"] == "evt-1"
    assert events[0]["summary"] == "Dentist"
    assert events[0]["location"] == "Geneva"
    assert events[0]["all_day"] is False
    assert events[1]["all_day"] is True
    assert events[1]["start_iso"] == "2026-05-18"


@pytest.mark.asyncio
async def test_create_event_posts_with_auth_header(integration, valid_tokens):
    await integration._persist_tokens(valid_tokens)

    fake_session = MagicMock()
    fake_session.closed = False
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    fake_session.post = MagicMock(return_value=_make_mock_response({
        "id": "evt-new",
        "summary": "Lunch",
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
    assert ev["summary"] == "Lunch"
    call_kwargs = fake_session.post.call_args.kwargs
    assert call_kwargs["headers"]["Authorization"] == "Bearer AT-fresh"


@pytest.mark.asyncio
async def test_disconnect_clears_creds(integration, valid_tokens):
    await integration._persist_tokens(valid_tokens)
    assert await integration.is_connected()

    fake_session = MagicMock()
    fake_session.closed = False
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    fake_session.post = MagicMock(return_value=_make_mock_response({}, status=200))

    with patch(
        "dragon_voice.tools.integrations.google.calendar.aiohttp.ClientSession",
        return_value=fake_session,
    ):
        await integration.disconnect()

    assert await integration.is_connected() is False
    assert not await integration._store.exists()


@pytest.mark.asyncio
async def test_expired_token_triggers_refresh_via_authcode_client(integration):
    """When `expires_at` is past, `_get_access_token` refreshes via the
    auth-code client and persists the new tokens."""
    expired = OAuthTokens(
        access_token="AT-OLD",
        refresh_token="RT-1",
        token_type="Bearer",
        expires_at=int(time.time()) - 10,
        scopes=["https://www.googleapis.com/auth/calendar.readonly"],
    )
    await integration._persist_tokens(expired)

    new_tokens = OAuthTokens(
        access_token="AT-NEW",
        refresh_token="RT-1",
        token_type="Bearer",
        expires_at=int(time.time()) + 3600,
        scopes=["https://www.googleapis.com/auth/calendar.readonly"],
    )

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
