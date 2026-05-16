"""Google OAuth (device-code grant) — shared by Calendar + Gmail.

Single Google OAuth client_id covers all Google services we use.  The
flow uses the device authorization grant per RFC 8628; Google's
endpoints:
  * device-code:  https://oauth2.googleapis.com/device/code
  * token:        https://oauth2.googleapis.com/token

The client_id (and optional client_secret) come from environment:
  * GOOGLE_OAUTH_CLIENT_ID — required for `start_connect` to work.
    Created by the user at https://console.cloud.google.com/ →
    APIs & Services → Credentials → Create OAuth client ID → "TVs and
    Limited Input devices" application type.
  * GOOGLE_OAUTH_CLIENT_SECRET — required when the OAuth client was
    issued one (Google's "TVs and Limited Input" type ships with both
    a client_id AND client_secret).  Optional otherwise.

When the env vars are missing, `start_connect` raises a structured
error so the Tab5 UI can surface a "Set up Google OAuth first"
explainer instead of failing silently.

This module is intentionally not an `IntegrationBackend` itself — it's
a shared auth helper that Calendar + Gmail backends compose.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from dragon_voice.tools.integrations.oauth import (
    DeviceCodeError,
    OAuthDeviceCodeClient,
    OAuthTokens,
)

logger = logging.getLogger(__name__)

# Google OAuth endpoints — per Google's official documentation for the
# OAuth 2.0 Device Authorization Grant.
GOOGLE_DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

# Scopes are space-separated when sent to Google.  The Calendar +
# Gmail backends each declare what they need; auth.py just collects.
SCOPE_CALENDAR_READ = "https://www.googleapis.com/auth/calendar.readonly"
SCOPE_CALENDAR_EVENTS = "https://www.googleapis.com/auth/calendar.events"
SCOPE_GMAIL_READ = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_GMAIL_MODIFY = "https://www.googleapis.com/auth/gmail.modify"
SCOPE_GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"


@dataclass
class GoogleOAuthConfig:
    """Provider-level OAuth config + scope union.

    `scopes` defaults to read-only Calendar — explicit at construction
    so a single Google credential file can grow scopes incrementally
    (Calendar first; then Gmail later expands the set).
    """

    client_id: str
    client_secret: Optional[str] = None
    scopes: list[str] = field(default_factory=lambda: [SCOPE_CALENDAR_READ])

    @classmethod
    def from_env(cls, scopes: Optional[list[str]] = None) -> "GoogleOAuthConfig":
        client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
        if not client_id:
            raise GoogleOAuthNotConfiguredError(
                "GOOGLE_OAUTH_CLIENT_ID env var is not set.  Create an "
                "OAuth client at https://console.cloud.google.com/ "
                '(application type "TVs and Limited Input devices") and '
                "set GOOGLE_OAUTH_CLIENT_ID + GOOGLE_OAUTH_CLIENT_SECRET "
                "in Dragon's environment."
            )
        client_secret = (
            os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip() or None
        )
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            scopes=list(scopes) if scopes is not None else [SCOPE_CALENDAR_READ],
        )

    def scope_string(self) -> str:
        return " ".join(self.scopes)


class GoogleOAuthNotConfiguredError(DeviceCodeError):
    """Raised when GOOGLE_OAUTH_CLIENT_ID is missing.

    Inherits DeviceCodeError so the REST layer can render the same
    error shape for "no client_id set" vs "user denied authorization".
    """

    def __init__(self, message: str) -> None:
        super().__init__(code="oauth_not_configured", description=message)


def make_google_client(config: GoogleOAuthConfig) -> OAuthDeviceCodeClient:
    """Build a Google-specific OAuthDeviceCodeClient."""
    return OAuthDeviceCodeClient(
        client_id=config.client_id,
        client_secret=config.client_secret,
        scope=config.scope_string(),
        device_code_url=GOOGLE_DEVICE_CODE_URL,
        token_url=GOOGLE_TOKEN_URL,
    )


async def revoke_google_token(token: str, session) -> bool:  # noqa: ANN001
    """POST to Google's revoke endpoint.  Returns True on 200.

    Per Google: revoking the refresh token invalidates BOTH the refresh
    token AND any access tokens minted from it — call this with the
    refresh_token when disconnecting.
    """
    try:
        async with session.post(
            GOOGLE_REVOKE_URL, data={"token": token},
        ) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        logger.warning("Google revoke endpoint unreachable", exc_info=True)
        return False


# Re-export OAuthTokens here so Calendar + Gmail can import a single
# Google-flavored auth module without poking into the generic oauth
# module.
__all__ = [
    "GoogleOAuthConfig",
    "GoogleOAuthNotConfiguredError",
    "OAuthTokens",
    "make_google_client",
    "revoke_google_token",
    "SCOPE_CALENDAR_READ",
    "SCOPE_CALENDAR_EVENTS",
    "SCOPE_GMAIL_READ",
    "SCOPE_GMAIL_MODIFY",
    "SCOPE_GMAIL_SEND",
    "GOOGLE_DEVICE_CODE_URL",
    "GOOGLE_TOKEN_URL",
    "GOOGLE_REVOKE_URL",
]
