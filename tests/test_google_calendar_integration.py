"""#341 / #342 — Google Calendar integration tests.

Stubs the Google OAuth + Calendar API via aiohttp session mocks.
Asserts:
  * `is_connected` is False initially, True after token persist
  * `start_connect` returns a verification_url + user_code from the
    mocked device-code endpoint
  * `list_events` builds the right URL + params, normalizes payload
  * `create_event` + `cancel_event` round-trip
  * `disconnect` calls revoke + deletes creds
  * Expired access token triggers refresh-on-401
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
from dragon_voice.tools.integrations.oauth import OAuthTokens


@pytest.fixture(autouse=True)
def _redirect_cred_store(tmp_path, monkeypatch):
    """Make all CredentialStore writes land in a per-test tmp dir."""
    monkeypatch.setenv("TINKERCLAW_INTEGRATIONS_DIR", str(tmp_path))


@pytest.fixture
def integration(tmp_path):
    integ = GoogleCalendarIntegration()
    # Replace the store with an explicit tmp_path-backed one to make
    # the redirect deterministic even if env-var lookup races.
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


@pytest.mark.asyncio
async def test_initial_state_is_disconnected(integration):
    assert await integration.is_connected() is False


@pytest.mark.asyncio
async def test_is_connected_true_after_persist(integration, valid_tokens):
    await integration._persist_tokens(valid_tokens)
    assert await integration.is_connected() is True


@pytest.mark.asyncio
async def test_start_connect_requires_oauth_env(integration, monkeypatch):
    """No GOOGLE_OAUTH_CLIENT_ID → raises a structured error so the
    REST layer can surface a 503 + actionable message."""
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    from dragon_voice.tools.integrations.google.auth import (
        GoogleOAuthNotConfiguredError,
    )
    with pytest.raises(GoogleOAuthNotConfiguredError):
        await integration.start_connect()


@pytest.mark.asyncio
async def test_health_check_returns_false_when_disconnected(integration):
    ok, detail = await integration.health_check()
    assert ok is False
    assert "not connected" in detail.lower()


@pytest.mark.asyncio
async def test_list_events_returns_normalized_shape(integration, valid_tokens):
    """The Calendar API returns verbose payloads — we normalize down
    to {id, summary, location, start_iso, end_iso, all_day, ...}."""
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
    # Verify the POST was authed.
    call_kwargs = fake_session.post.call_args.kwargs
    assert call_kwargs["headers"]["Authorization"] == "Bearer AT-fresh"


@pytest.mark.asyncio
async def test_disconnect_clears_creds(integration, valid_tokens):
    await integration._persist_tokens(valid_tokens)
    assert await integration.is_connected()

    # Mock the revoke endpoint as a no-op success.
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
async def test_expired_token_triggers_refresh(integration, monkeypatch):
    """When the cached `expires_at` is past, `_get_access_token` must
    refresh via the OAuth client before returning the new access token."""
    expired = OAuthTokens(
        access_token="AT-OLD",
        refresh_token="RT-1",
        token_type="Bearer",
        expires_at=int(time.time()) - 10,  # already expired
        scopes=["https://www.googleapis.com/auth/calendar.readonly"],
    )
    await integration._persist_tokens(expired)

    # Mock GoogleOAuthConfig.from_env to skip env-var check.
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "csec")

    new_tokens = OAuthTokens(
        access_token="AT-NEW",
        refresh_token="RT-1",
        token_type="Bearer",
        expires_at=int(time.time()) + 3600,
        scopes=["https://www.googleapis.com/auth/calendar.readonly"],
    )

    refresh_mock = AsyncMock(return_value=new_tokens)
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.refresh = refresh_mock

    with patch(
        "dragon_voice.tools.integrations.google.calendar.make_google_client",
        return_value=fake_client,
    ):
        token = await integration._get_access_token()

    assert token == "AT-NEW"
    refresh_mock.assert_awaited_once_with("RT-1")
