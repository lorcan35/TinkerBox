"""Google OAuth helpers — shared by Calendar + Gmail.

Pivoted from device-code to **authorization-code-with-PKCE** because
Google's device-code allowed-scope list excludes Calendar + Gmail.
See ``../oauth.py`` module docstring for the explanation.

Single Google Web-application OAuth client (created in Google Cloud
Console) is reused across all Google integrations.  The redirect URI
must match what's registered in the Console:
``https://tinkerclaw-voice.ngrok.dev/api/v1/oauth/callback``
(reachable from any device via the existing TinkerClaw ngrok tunnel).

Env vars expected on Dragon:

* ``GOOGLE_OAUTH_CLIENT_ID`` — required
* ``GOOGLE_OAUTH_CLIENT_SECRET`` — required for web-application client
* ``GOOGLE_OAUTH_REDIRECT_URI`` — optional override; defaults to the
  ngrok URL.

Multi-account support (#347): every Google integration also requests
``openid email`` so the token-exchange response includes an ``id_token``
JWT whose `email` claim becomes the account_id.  See
``extract_account_id_from_tokens``.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from dragon_voice.tools.integrations.oauth import (
    DeviceCodeError,
    OAuthAuthCodeClient,
    OAuthTokens,
)

logger = logging.getLogger(__name__)

# Endpoints.
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

# Default redirect — ngrok tunnel that the existing tinkerclaw-ngrok
# service maintains pointing at Dragon's port 3502.  Operator can
# override via env var if running on a different network setup.
DEFAULT_REDIRECT_URI = (
    "https://tinkerclaw-voice.ngrok.dev/api/v1/oauth/callback"
)

# Identity scopes — added to every Google flow so we can extract the
# account email from the id_token without a second round trip.
SCOPE_OPENID = "openid"
SCOPE_EMAIL = "email"

# Per-integration scopes.
SCOPE_CALENDAR_READ = "https://www.googleapis.com/auth/calendar.readonly"
SCOPE_CALENDAR_EVENTS = "https://www.googleapis.com/auth/calendar.events"
SCOPE_GMAIL_READ = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_GMAIL_MODIFY = "https://www.googleapis.com/auth/gmail.modify"
SCOPE_GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"


class GoogleOAuthNotConfiguredError(DeviceCodeError):
    """Raised when env vars are missing.  Inherits DeviceCodeError so
    REST handlers render a consistent error shape across all OAuth
    failures."""

    def __init__(self, message: str) -> None:
        super().__init__(code="oauth_not_configured", description=message)


@dataclass
class GoogleOAuthConfig:
    """Provider-level OAuth config + scope union."""

    client_id: str
    client_secret: Optional[str]
    redirect_uri: str
    scopes: list[str] = field(default_factory=lambda: [SCOPE_CALENDAR_READ])

    @classmethod
    def from_env(cls, scopes: Optional[list[str]] = None) -> "GoogleOAuthConfig":
        client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
        client_secret = os.environ.get(
            "GOOGLE_OAUTH_CLIENT_SECRET", "",
        ).strip() or None
        redirect_uri = (
            os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "").strip()
            or DEFAULT_REDIRECT_URI
        )
        if not client_id:
            raise GoogleOAuthNotConfiguredError(
                "GOOGLE_OAUTH_CLIENT_ID is not set.  Create a Web-application "
                "OAuth client at https://console.cloud.google.com/apis/credentials "
                "(redirect URI: " + redirect_uri + ") then set "
                "GOOGLE_OAUTH_CLIENT_ID + GOOGLE_OAUTH_CLIENT_SECRET in Dragon's "
                "environment and restart tinkerclaw-voice.",
            )
        merged_scopes = list(scopes) if scopes is not None else [SCOPE_CALENDAR_READ]
        # Always include identity scopes so we can extract the account
        # email from the id_token after exchange.
        for s in (SCOPE_OPENID, SCOPE_EMAIL):
            if s not in merged_scopes:
                merged_scopes.append(s)
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scopes=merged_scopes,
        )

    def scope_string(self) -> str:
        return " ".join(self.scopes)


def make_google_client(config: GoogleOAuthConfig) -> OAuthAuthCodeClient:
    """Build a Google-flavored authorization-code-with-PKCE client."""
    return OAuthAuthCodeClient(
        client_id=config.client_id,
        client_secret=config.client_secret,
        scope=config.scope_string(),
        auth_url=GOOGLE_AUTH_URL,
        token_url=GOOGLE_TOKEN_URL,
        redirect_uri=config.redirect_uri,
    )


def extract_account_id_from_tokens(tokens: OAuthTokens) -> Optional[str]:
    """Decode the id_token JWT and return the `email` claim.

    The id_token comes back from Google's token endpoint when the
    request included `openid email` in the scope set.  We don't verify
    the signature — the token was just delivered over TLS straight
    from Google's `oauth2.googleapis.com`, so payload trust is
    transitive.  Returns None when:

      * the response had no id_token (caller should fall back to the
        userinfo endpoint or use a placeholder account_id)
      * the JWT is malformed
      * the email_verified claim is False (we won't use unverified
        emails as account_ids — they could collide between accounts)
    """
    id_token = (tokens.extra or {}).get("id_token") if tokens.extra else None
    if not id_token or not isinstance(id_token, str):
        return None
    parts = id_token.split(".")
    if len(parts) != 3:
        return None
    try:
        # base64url-decode the payload — JWT bodies are url-safe b64
        # without padding.
        payload_raw = parts[1]
        # Re-pad for base64.urlsafe_b64decode.
        payload_raw += "=" * (-len(payload_raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_raw))
    except (ValueError, json.JSONDecodeError):
        return None
    email = payload.get("email")
    if not email or payload.get("email_verified") is False:
        return None
    return str(email).lower()


async def fetch_google_email(access_token: str, session) -> Optional[str]:  # noqa: ANN001
    """Fallback: hit the userinfo endpoint when id_token wasn't present.

    Returns the verified email or None on any failure (caller treats
    None as "use a placeholder account_id").
    """
    try:
        async with session.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        ) as resp:
            if resp.status != 200:
                logger.warning(
                    "Google userinfo returned %s — token may lack "
                    "openid/email scope.  Falling back to placeholder.",
                    resp.status,
                )
                return None
            data = await resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("Google userinfo unreachable: %s", e)
        return None
    email = data.get("email")
    if not email or data.get("email_verified") is False:
        return None
    return str(email).lower()


async def revoke_google_token(token: str, session) -> bool:  # noqa: ANN001
    """POST to Google's revoke endpoint.  Returns True on 200.

    Per Google: revoking the refresh token invalidates both the refresh
    token AND any active access tokens minted from it.
    """
    try:
        async with session.post(
            GOOGLE_REVOKE_URL, data={"token": token},
        ) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        logger.warning("Google revoke endpoint unreachable", exc_info=True)
        return False


__all__ = [
    "DEFAULT_REDIRECT_URI",
    "GOOGLE_AUTH_URL",
    "GOOGLE_REVOKE_URL",
    "GOOGLE_TOKEN_URL",
    "GOOGLE_USERINFO_URL",
    "GoogleOAuthConfig",
    "GoogleOAuthNotConfiguredError",
    "OAuthTokens",
    "SCOPE_CALENDAR_EVENTS",
    "SCOPE_CALENDAR_READ",
    "SCOPE_EMAIL",
    "SCOPE_GMAIL_MODIFY",
    "SCOPE_GMAIL_READ",
    "SCOPE_GMAIL_SEND",
    "SCOPE_OPENID",
    "extract_account_id_from_tokens",
    "fetch_google_email",
    "make_google_client",
    "revoke_google_token",
]
