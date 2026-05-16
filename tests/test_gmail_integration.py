"""#341 / Phase 2 — Gmail integration tests (multi-account, auth-code).

Stubs the Google OAuth + Gmail API via aiohttp session mocks.  Covers
the shape (multi-account, auth-code), list_messages + get_message_body
+ send_message + modify_labels round-trips, and account routing.

Mirrors test_google_calendar_integration.py — same patterns, same
fixtures, different API surface.
"""

from __future__ import annotations

import base64
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dragon_voice.tools.integrations.credentials import ProviderCredentialDir
from dragon_voice.tools.integrations.google.gmail import (
    PROVIDER_NAME,
    GmailIntegration,
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
    integ = GmailIntegration()
    integ._provider_dir = ProviderCredentialDir(PROVIDER_NAME, base_dir=tmp_path)
    return integ


def _tokens(access="AT-1", refresh="RT-1", id_email=None):
    extra = {}
    if id_email:
        payload = {
            "email": id_email, "email_verified": True, "sub": "1234567890",
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
        scopes=[
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.send",
            "openid", "email",
        ],
        extra=extra,
    )


def _resp(json_data, status=200):
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=MagicMock(
        status=status,
        json=AsyncMock(return_value=json_data),
        raise_for_status=MagicMock(),
    ))
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


async def _persist(integ, account_id, tokens, default=False):
    await integ._ensure_loaded()
    if tokens.extra is None:
        tokens.extra = {}
    tokens.extra["default"] = default
    await integ._persist_account(account_id, tokens)
    integ._accounts[account_id] = tokens
    if default or integ._default_account_id is None:
        integ._default_account_id = account_id


# ── shape ───────────────────────────────────────────────────────────


def test_provider_metadata(integration):
    assert integration.name == "gmail"
    assert integration.display_name == "Gmail"
    assert integration.auth_kind == "oauth-authcode"
    assert integration.supports_multi_account is True


# ── lifecycle ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disconnected_initially(integration):
    assert await integration.is_connected() is False
    assert await integration.list_accounts() == []


@pytest.mark.asyncio
async def test_start_connect_returns_pkce_url(integration):
    chal = await integration.start_connect()
    assert chal.kind == "oauth-authcode"
    # All three gmail scopes plus openid+email.
    assert "gmail.readonly" in chal.verification_url
    assert "gmail.modify" in chal.verification_url
    assert "gmail.send" in chal.verification_url
    assert "openid" in chal.verification_url


@pytest.mark.asyncio
async def test_handle_callback_uses_id_token_email(integration):
    chal = await integration.start_connect()
    flow = integration._flows[chal.request_id]
    tokens = _tokens(id_email="me@example.com")

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.exchange_code_with_verifier = AsyncMock(return_value=tokens)

    with patch(
        "dragon_voice.tools.integrations.google.gmail.make_google_client",
        return_value=fake_client,
    ):
        await integration.handle_callback(
            state=flow.state, code="AUTH-1", error=None,
        )

    accounts = await integration.list_accounts()
    assert [a.account_id for a in accounts] == ["me@example.com"]
    assert accounts[0].default is True


# ── list_messages ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_messages_fans_out_metadata(integration):
    await _persist(integration, "me@example.com", _tokens(), default=True)

    fake_session = MagicMock()
    fake_session.closed = False
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)

    # First call: list endpoint returns 2 ids.  Then 2 metadata fetches.
    responses = [
        _resp({"messages": [{"id": "m1"}, {"id": "m2"}]}),
        _resp({
            "id": "m1", "snippet": "first",
            "labelIds": ["INBOX", "UNREAD"],
            "payload": {"headers": [
                {"name": "From", "value": "alice@example.com"},
                {"name": "Subject", "value": "hi"},
                {"name": "Date", "value": "Sat, 17 May 2026 10:00:00 +0000"},
            ]},
        }),
        _resp({
            "id": "m2", "snippet": "second",
            "labelIds": ["INBOX"],
            "payload": {"headers": [
                {"name": "From", "value": "bob@example.com"},
                {"name": "Subject", "value": "follow-up"},
                {"name": "Date", "value": "Sat, 17 May 2026 11:00:00 +0000"},
            ]},
        }),
    ]
    fake_session.get = MagicMock(side_effect=responses)

    with patch(
        "dragon_voice.tools.integrations.google.gmail.aiohttp.ClientSession",
        return_value=fake_session,
    ):
        messages = await integration.list_messages(query="is:unread")

    assert len(messages) == 2
    by_id = {m["id"]: m for m in messages}
    assert by_id["m1"]["from"] == "alice@example.com"
    assert by_id["m1"]["subject"] == "hi"
    assert by_id["m1"]["unread"] is True
    assert by_id["m2"]["unread"] is False
    assert by_id["m1"]["account"] == "me@example.com"


@pytest.mark.asyncio
async def test_list_messages_empty_when_no_matches(integration):
    await _persist(integration, "me@example.com", _tokens(), default=True)

    fake_session = MagicMock()
    fake_session.closed = False
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    fake_session.get = MagicMock(return_value=_resp({}))

    with patch(
        "dragon_voice.tools.integrations.google.gmail.aiohttp.ClientSession",
        return_value=fake_session,
    ):
        messages = await integration.list_messages(query="from:nobody")

    assert messages == []


# ── get_message_body ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_message_body_extracts_text_plain(integration):
    await _persist(integration, "me@example.com", _tokens(), default=True)

    plain_body = base64.urlsafe_b64encode(b"Hello world.\n").rstrip(b"=").decode("ascii")
    fake_session = MagicMock()
    fake_session.closed = False
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    fake_session.get = MagicMock(return_value=_resp({
        "id": "m1", "snippet": "Hello world.",
        "labelIds": ["INBOX"],
        "payload": {
            "headers": [
                {"name": "From", "value": "alice@example.com"},
                {"name": "Subject", "value": "Hi"},
                {"name": "Date", "value": "Sat, 17 May 2026 10:00:00 +0000"},
            ],
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": plain_body}},
                {"mimeType": "text/html", "body": {"data": ""}},
            ],
        },
    }))

    with patch(
        "dragon_voice.tools.integrations.google.gmail.aiohttp.ClientSession",
        return_value=fake_session,
    ):
        msg = await integration.get_message_body("m1")

    assert msg["body_text"] == "Hello world.\n"
    assert msg["from"] == "alice@example.com"
    assert msg["subject"] == "Hi"
    assert msg["account"] == "me@example.com"


@pytest.mark.asyncio
async def test_get_message_body_handles_single_part(integration):
    """Single-part messages have body.data directly under payload."""
    await _persist(integration, "me@example.com", _tokens(), default=True)

    plain_body = base64.urlsafe_b64encode(b"single part text").rstrip(b"=").decode("ascii")
    fake_session = MagicMock()
    fake_session.closed = False
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    fake_session.get = MagicMock(return_value=_resp({
        "id": "m1", "snippet": "single part text",
        "labelIds": ["INBOX"],
        "payload": {
            "headers": [
                {"name": "From", "value": "alice@example.com"},
                {"name": "Subject", "value": "Single"},
            ],
            "mimeType": "text/plain",
            "body": {"data": plain_body},
        },
    }))

    with patch(
        "dragon_voice.tools.integrations.google.gmail.aiohttp.ClientSession",
        return_value=fake_session,
    ):
        msg = await integration.get_message_body("m1")

    assert msg["body_text"] == "single part text"


# ── send_message ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_message_posts_base64url_raw(integration):
    await _persist(integration, "me@example.com", _tokens(), default=True)

    fake_session = MagicMock()
    fake_session.closed = False
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    fake_session.post = MagicMock(return_value=_resp({
        "id": "sent-id", "threadId": "thread-id", "labelIds": ["SENT"],
    }))

    with patch(
        "dragon_voice.tools.integrations.google.gmail.aiohttp.ClientSession",
        return_value=fake_session,
    ):
        resp = await integration.send_message(
            to="alice@example.com",
            subject="Hello",
            body="Greetings from Tab5.",
        )

    assert resp["sent"] is True
    assert resp["id"] == "sent-id"
    assert resp["thread_id"] == "thread-id"
    # Verify the POST contained the RFC 822 raw payload.
    payload = fake_session.post.call_args.kwargs["json"]
    assert "raw" in payload
    decoded = base64.urlsafe_b64decode(payload["raw"] + "==").decode("utf-8", errors="replace")
    assert "To: alice@example.com" in decoded
    assert "Subject: Hello" in decoded
    assert "Greetings from Tab5." in decoded


@pytest.mark.asyncio
async def test_send_message_in_reply_to_threads_correctly(integration):
    await _persist(integration, "me@example.com", _tokens(), default=True)

    fake_session = MagicMock()
    fake_session.closed = False
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    # First GET fetches the original (Message-Id header + threadId),
    # then POST sends the reply.
    fake_session.get = MagicMock(return_value=_resp({
        "id": "orig", "threadId": "T-99",
        "payload": {"headers": [
            {"name": "Message-Id", "value": "<orig@example.com>"},
        ]},
    }))
    fake_session.post = MagicMock(return_value=_resp({
        "id": "reply-id", "threadId": "T-99",
    }))

    with patch(
        "dragon_voice.tools.integrations.google.gmail.aiohttp.ClientSession",
        return_value=fake_session,
    ):
        resp = await integration.send_message(
            to="alice@example.com",
            subject="Re: Hi",
            body="yes",
            in_reply_to="orig",
        )

    assert resp["thread_id"] == "T-99"
    payload = fake_session.post.call_args.kwargs["json"]
    assert payload.get("threadId") == "T-99"
    decoded = base64.urlsafe_b64decode(payload["raw"] + "==").decode("utf-8", errors="replace")
    assert "In-Reply-To: <orig@example.com>" in decoded


# ── modify_labels (archive etc.) ───────────────────────────────────


@pytest.mark.asyncio
async def test_modify_labels_archive(integration):
    await _persist(integration, "me@example.com", _tokens(), default=True)

    fake_session = MagicMock()
    fake_session.closed = False
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    fake_session.post = MagicMock(return_value=_resp({
        "id": "m1", "labelIds": ["UNREAD"],
    }))

    with patch(
        "dragon_voice.tools.integrations.google.gmail.aiohttp.ClientSession",
        return_value=fake_session,
    ):
        resp = await integration.modify_labels("m1", remove=["INBOX"])

    assert resp["modified"] is True
    assert "INBOX" not in resp["labels"]
    body = fake_session.post.call_args.kwargs["json"]
    assert body == {"removeLabelIds": ["INBOX"]}


@pytest.mark.asyncio
async def test_modify_labels_noop_when_nothing_supplied(integration):
    await _persist(integration, "me@example.com", _tokens(), default=True)
    resp = await integration.modify_labels("m1")
    assert resp["modified"] is False


# ── multi-account routing ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_messages_routes_to_explicit_account(integration):
    await _persist(integration, "first@example.com", _tokens(access="AT-FIRST"), default=True)
    await _persist(integration, "second@example.com", _tokens(access="AT-SECOND"))

    fake_session = MagicMock()
    fake_session.closed = False
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    fake_session.get = MagicMock(return_value=_resp({}))

    with patch(
        "dragon_voice.tools.integrations.google.gmail.aiohttp.ClientSession",
        return_value=fake_session,
    ):
        await integration.list_messages(
            query="is:unread", account_id="second@example.com",
        )

    headers = fake_session.get.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer AT-SECOND"


@pytest.mark.asyncio
async def test_disconnect_one_account_leaves_others(integration):
    await _persist(integration, "first@example.com", _tokens(refresh=None), default=True)
    await _persist(integration, "second@example.com", _tokens(refresh=None))

    await integration.disconnect(account_id="first@example.com")

    accounts = await integration.list_accounts()
    assert [a.account_id for a in accounts] == ["second@example.com"]
    assert accounts[0].default is True


@pytest.mark.asyncio
async def test_set_default_account_unknown_raises(integration):
    await _persist(integration, "first@example.com", _tokens(), default=True)
    with pytest.raises(DeviceCodeError) as excinfo:
        await integration.set_default_account("ghost@example.com")
    assert excinfo.value.code == "unknown_account"
